#!/usr/bin/env python3
"""
tools/anidb_client.py

Standalone TUI for the anime provider chain:

    Jikan (search + episode list) -> Kitsu (episode enrichment, via the
    public mappings API -- no local mapping.db needed) -> AniDBProvider
    (source resolution + stream extraction)

Doesn't touch the FastAPI app, CacheEngine, or mapping.db. Just the raw
provider chain, so you can point it at a title and see exactly where it
breaks -- useful for the anidb.app scraper 404 debugging.

Usage:
    python tools/anidb_client.py

Controls:
    Type              search (auto-fires ~350ms after you stop typing)
    Up/Down           navigate results / episodes
    Enter             select
    d                 toggle dub/sub                    (episode screen)
    Backspace         edit query (search) / go back      (other screens)
    Esc                                                   quit
"""

import asyncio
import os
import sys
import tty
import termios
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import httpx

# Make `app.*` importable regardless of cwd -- this file lives at
# <repo_root>/app/tools/anidb_test.py, so repo_root is two levels up.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.providers.anime.anidb import AniDBProvider
from app.providers.metadata.jikan import get_jikan_episodes
from app.providers.metadata.kitsu import enrich_episodes_with_kitsu
from app.models.media import EpisodeShort

JIKAN_SEARCH_URL = "https://api.jikan.moe/v4/anime"
KITSU_MAPPINGS_URL = "https://kitsu.io/api/edge/mappings"

ESC = "\x1b"
CLEAR = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
REVERSE = "\x1b[7m"
RESET = "\x1b[0m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ==========================================
# RAW TERMINAL HANDLING
# ==========================================

class RawTerminal:
    """Context manager: cbreak mode + hidden cursor, always restored."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        print(HIDE_CURSOR, end="", flush=True)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        print(SHOW_CURSOR, end="", flush=True)


class KeyReader:
    """
    Feeds keypresses into an asyncio.Queue via loop.add_reader, collapsing
    arrow-key escape sequences to 'UP'/'DOWN'/'LEFT'/'RIGHT'.

    Simplification: when the first byte is ESC, the next 2 bytes are read
    with a blocking os.read() inside the callback. Real terminals deliver
    all 3 bytes of an arrow sequence together, so in practice this doesn't
    stall the loop -- fine for a single-user debug tool, not something
    you'd want in a server.
    """

    def __init__(self):
        self.queue: "asyncio.Queue[str]" = asyncio.Queue()
        self.fd = sys.stdin.fileno()

    def start(self):
        asyncio.get_event_loop().add_reader(self.fd, self._on_readable)

    def stop(self):
        try:
            asyncio.get_event_loop().remove_reader(self.fd)
        except Exception:
            pass

    def _on_readable(self):
        ch = os.read(self.fd, 1).decode(errors="ignore")
        if not ch:
            return
        if ch == ESC:
            rest = os.read(self.fd, 2).decode(errors="ignore")
            mapping = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}
            self.queue.put_nowait(mapping.get(rest, "ESC"))
        elif ch in ("\r", "\n"):
            self.queue.put_nowait("ENTER")
        elif ch in ("\x7f", "\x08"):
            self.queue.put_nowait("BACKSPACE")
        elif ch == "\x03":  # Ctrl-C
            self.queue.put_nowait("ESC")
        else:
            self.queue.put_nowait(ch)


# ==========================================
# DATA
# ==========================================

@dataclass
class AnimeResult:
    mal_id: int
    title: str
    title_english: Optional[str]
    year: Optional[int]
    type: Optional[str]
    episodes: Optional[int]
    score: Optional[float]

    @property
    def display_title(self) -> str:
        return self.title_english or self.title


# ==========================================
# NETWORK (standalone -- no app/DB dependency)
# ==========================================

async def search_jikan(client: httpx.AsyncClient, query: str) -> List[AnimeResult]:
    if not query.strip():
        return []
    try:
        resp = await client.get(JIKAN_SEARCH_URL, params={
            "q": query, "limit": 8, "order_by": "popularity", "sort": "asc",
        })
        if resp.status_code != 200:
            return []
        out = []
        for item in resp.json().get("data", []):
            out.append(AnimeResult(
                mal_id=item["mal_id"],
                title=item.get("title"),
                title_english=item.get("title_english"),
                year=item.get("year"),
                type=item.get("type"),
                episodes=item.get("episodes"),
                score=item.get("score"),
            ))
        return out
    except Exception:
        return []


async def resolve_kitsu_id(client: httpx.AsyncClient, mal_id: int) -> Optional[int]:
    """MAL -> Kitsu id via Kitsu's public mappings API. This is what
    mapping_engine.get_kitsu_id_from_mal() does via the local Fribb DB --
    same result, zero DB dependency, so this tool stays standalone."""
    try:
        resp = await client.get(KITSU_MAPPINGS_URL, params={
            "filter[externalSite]": "myanimelist/anime",
            "filter[externalId]": mal_id,
        })
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data:
            return None
        item = data[0].get("relationships", {}).get("item", {}).get("data", {})
        return int(item["id"]) if item.get("id") else None
    except Exception:
        return None


# ==========================================
# APP
# ==========================================

class App:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.key_reader = KeyReader()
        self.running = True
        self.screen = "search"  # search | episodes | result

        self.query = ""
        self.results: List[AnimeResult] = []
        self.selected = 0
        self._debounce_task: Optional[asyncio.Task] = None

        self.current_anime: Optional[AnimeResult] = None
        self.episodes: List[EpisodeShort] = []
        self.ep_selected = 0
        self.is_dub = False

        self.result_lines: List[str] = []

        self.spinner_active = False
        self._spinner_idx = 0
        self._spin_task: Optional[asyncio.Task] = None

    # ---------- spinner ----------

    def _spinner_frame(self) -> str:
        return SPINNER_FRAMES[self._spinner_idx]

    async def _spin_while_active(self):
        try:
            while self.spinner_active:
                self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)
                self.render()
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            pass

    async def _with_spinner(self, coro):
        self.spinner_active = True
        self.render()
        self._spin_task = asyncio.create_task(self._spin_while_active())
        try:
            return await coro
        finally:
            self.spinner_active = False
            self._spin_task.cancel()

    # ---------- search screen ----------

    def on_query_changed(self):
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_search())

    async def _debounced_search(self):
        try:
            await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            return
        self.results = await self._with_spinner(search_jikan(self.client, self.query))
        self.selected = 0
        self.render()

    async def handle_key_search(self, key: str):
        if key == "UP":
            self.selected = max(0, self.selected - 1)
        elif key == "DOWN":
            if self.results:
                self.selected = min(len(self.results) - 1, self.selected + 1)
        elif key == "ENTER":
            if self.results:
                await self.enter_episodes(self.results[self.selected])
        elif key == "BACKSPACE":
            self.query = self.query[:-1]
            self.on_query_changed()
        elif len(key) == 1 and key.isprintable():
            self.query += key
            self.selected = 0
            self.on_query_changed()

    def render_search(self):
        lines = [CLEAR]
        lines.append(f"{BOLD}{CYAN}AniDB Provider Chain -- Search{RESET}")
        lines.append(f"{DIM}Jikan search -> Kitsu enrich -> AniDB resolve. Esc to quit.{RESET}")
        lines.append("")
        lines.append(f"Search: {self.query}{'' if self.spinner_active else '_'}")
        if self.spinner_active:
            lines.append(f"{YELLOW}{self._spinner_frame()} searching...{RESET}")
        lines.append("")
        if not self.results and self.query and not self.spinner_active:
            lines.append(f"{DIM}No results.{RESET}")
        for i, r in enumerate(self.results):
            marker = f"{REVERSE} > {RESET}" if i == self.selected else "   "
            meta = f"{DIM}{r.type or '?'} · {r.year or '????'} · {r.episodes or '?'} eps · \u2605{r.score or '-'}{RESET}"
            title = f"{BOLD}{r.display_title}{RESET}" if i == self.selected else r.display_title
            lines.append(f"{marker}{title}  {meta}")
        sys.stdout.write("\r\n".join(lines) + "\r\n")
        sys.stdout.flush()

    # ---------- episodes screen ----------

    async def enter_episodes(self, anime: AnimeResult):
        self.current_anime = anime
        self.episodes = []
        self.ep_selected = 0
        self.screen = "episodes"

        async def fetch():
            eps = await get_jikan_episodes(anime.mal_id, custom_id=f"m{anime.mal_id}")
            kitsu_id = await resolve_kitsu_id(self.client, anime.mal_id)
            if kitsu_id:
                await enrich_episodes_with_kitsu(eps, kitsu_id)
            return eps

        self.episodes = await self._with_spinner(fetch())
        self.render()

    async def handle_key_episodes(self, key: str):
        if key == "UP":
            self.ep_selected = max(0, self.ep_selected - 1)
        elif key == "DOWN":
            if self.episodes:
                self.ep_selected = min(len(self.episodes) - 1, self.ep_selected + 1)
        elif key == "d":
            self.is_dub = not self.is_dub
        elif key == "ENTER":
            if self.episodes:
                await self.resolve_source(self.episodes[self.ep_selected])
        elif key == "BACKSPACE":
            self.screen = "search"

    def render_episodes(self):
        lines = [CLEAR]
        title = self.current_anime.display_title if self.current_anime else "?"
        mal_id = self.current_anime.mal_id if self.current_anime else "?"
        lines.append(f"{BOLD}{CYAN}{title}{RESET}  {DIM}(MAL {mal_id}){RESET}")
        audio = "DUB" if self.is_dub else "SUB"
        lines.append(f"{DIM}Audio: {audio} (d to toggle)  ·  Backspace back  ·  Esc quit{RESET}")
        lines.append("")
        if self.spinner_active:
            lines.append(f"{YELLOW}{self._spinner_frame()} loading episodes...{RESET}")
        elif not self.episodes:
            lines.append(f"{DIM}No episodes found (Jikan returned nothing for this id).{RESET}")
        for i, ep in enumerate(self.episodes):
            marker = f"{REVERSE} > {RESET}" if i == self.ep_selected else "   "
            filler = f" {YELLOW}[filler]{RESET}" if ep.is_filler else ""
            ep_title = f"{BOLD}{ep.title}{RESET}" if i == self.ep_selected else ep.title
            lines.append(f"{marker}EP {ep.episode_number:>3}  {ep_title}{filler}")
        sys.stdout.write("\r\n".join(lines) + "\r\n")
        sys.stdout.flush()

    # ---------- result screen ----------

    async def resolve_source(self, ep: EpisodeShort):
        self.screen = "result"
        self.result_lines = []

        async def resolve():
            lines = []
            try:
                provider = AniDBProvider()
                offers = await provider.get_source_offers(
                    mapped_id=f"m{self.current_anime.mal_id}",
                    episode_absolute=ep.absolute_number,
                    is_dub=self.is_dub,
                )
                if not offers:
                    lines = [
                        f"{RED}No source found.{RESET}",
                        f"{DIM}AniDBProvider returned zero offers -- either the title match",
                        f"failed on anidb.app's search, or the episode index didn't resolve.",
                        f"Check stdout above for any [AniDB] error prints.{RESET}",
                    ]
                    return lines

                offer = offers[0]
                qs = urllib.parse.urlparse(offer.url).query
                ep_id = urllib.parse.parse_qs(qs).get("ep_id", [None])[0]
                if not ep_id:
                    return [f"{RED}Offer had no ep_id in its payload URL: {offer.url}{RESET}"]

                stream = await provider.extract_stream(ep_id)
                referer = (stream.headers or {}).get("Referer", "")
                lines = [
                    f"{GREEN}Resolved.{RESET}",
                    f"{BOLD}Stream URL:{RESET} {stream.stream_url}",
                    f"{BOLD}Type:{RESET} {stream.stream_type}",
                    f"{BOLD}Headers:{RESET} {stream.headers}",
                    "",
                    f"{DIM}mpv --http-header-fields=\"Referer: {referer}\" \"{stream.stream_url}\"{RESET}",
                ]
            except Exception as e:
                lines = [f"{RED}Error: {e}{RESET}"]
            return lines

        self.result_lines = await self._with_spinner(resolve())
        self.render()

    async def handle_key_result(self, key: str):
        if key in ("ENTER", "BACKSPACE"):
            self.screen = "episodes"

    def render_result(self):
        lines = [CLEAR]
        lines.append(f"{BOLD}{CYAN}Source Resolution{RESET}")
        lines.append(f"{DIM}Backspace back  ·  Esc quit{RESET}")
        lines.append("")
        if self.spinner_active:
            lines.append(f"{YELLOW}{self._spinner_frame()} resolving via AniDB...{RESET}")
        else:
            lines.extend(self.result_lines)
        sys.stdout.write("\r\n".join(lines) + "\r\n")
        sys.stdout.flush()

    # ---------- dispatch ----------

    def render(self):
        if self.screen == "search":
            self.render_search()
        elif self.screen == "episodes":
            self.render_episodes()
        elif self.screen == "result":
            self.render_result()

    async def handle_key(self, key: str):
        if key == "ESC":
            self.running = False
            return
        if self.screen == "search":
            await self.handle_key_search(key)
        elif self.screen == "episodes":
            await self.handle_key_episodes(key)
        elif self.screen == "result":
            await self.handle_key_result(key)
        self.render()

    async def run(self):
        self.render()
        while self.running:
            key = await self.key_reader.queue.get()
            await self.handle_key(key)


async def main():
    async with httpx.AsyncClient(timeout=10.0) as client:
        app = App(client)
        with RawTerminal():
            app.key_reader.start()
            try:
                await app.run()
            finally:
                app.key_reader.stop()
    print(CLEAR + "Bye.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(SHOW_CURSOR)