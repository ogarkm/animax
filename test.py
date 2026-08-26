"""
ani_test.py — Standalone diagnostic for AniDBProvider (sub/dub + subtitle availability).

Drop this in your project ROOT (next to the `app/` package) and run it there —
it imports the REAL AniDBProvider class from your codebase, so results reflect
actual production behavior rather than a reimplementation that could drift
from the real thing.

Every HTTP request and response made through the provider's own httpx client
is logged to `ani_test_network.log` (method, URL, headers, status, timing,
and body — bodies over LOG_BODY_LIMIT chars are truncated in the log but the
full raw body of every /languages call and every embed page is additionally
saved to individual files under ./ani_test_output/ so you can inspect them
directly, since I can't reach anidb.app from where I'm running to see this
myself).

USAGE
    python ani_test.py --anilist-id 176496 --episode 1
    python ani_test.py --mal-id 58567 --episode 12 --dub-only
    python ani_test.py --anilist-id 176496 --episode 1 --episode 2 --episode 3

What it checks, in order, for each requested episode:
  1. get_source_offers(is_dub=False) and (is_dub=True) — does AniDB even
     resolve an offer for both, or does one silently fail?
  2. The RAW /api/frontend/episode/{id}/languages response — every language
     code anidb.app actually has for this episode, not just eng/jpn. This is
     the single most important piece of evidence: if there's no "jpn" entry
     at all, or if "jpn" and "fra" are somehow pointing at the same
     embed_url, that's the smoking gun for wrong audio.
  3. The raw embed HTML for every available language (saved to a file each,
     since these pages can be large) — so you/I can see whether a subtitle
     track array (e.g. a jwplayer-style `tracks: [...]`) actually exists
     alongside the video `sources: [...]` that the current code extracts.
  4. extract_stream() for both is_dub values — the actual stream_url and
     track list AniDBProvider would hand to the player right now.

Nothing here calls Videasy/TMDB — this is AniDB-only, since that's the path
you're actually watching through and the "French audio" claim.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# --- Make sure we're importing the REAL app code, not reimplementing it ---
# Assumes this script sits next to the `app/` package (project root).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import httpx
except ImportError:
    print("httpx is required: pip install httpx")
    sys.exit(1)

try:
    from app.providers.anime.anidb import AniDBProvider
except Exception as e:
    print(f"Could not import AniDBProvider from app.providers.anime.anidb: {e}")
    print("Make sure this script is placed at your project root, next to the 'app' folder,")
    print("and that you're running it with the same Python environment as the app.")
    sys.exit(1)

try:
    from app.models.player import MediaContext
except Exception:
    MediaContext = None  # context is optional; we can run without it

OUTPUT_DIR = Path("./ani_test_output")
NETWORK_LOG_PATH = Path("./ani_test_network.log")
LOG_BODY_LIMIT = 6000  # chars kept inline in the network log; full bodies are saved separately for key calls

# ---------------------------------------------------------------------------
# Logging setup: one logger for the human-readable run narrative (stdout +
# ani_test.log), one dedicated logger for raw request/response pairs
# (ani_test_network.log), so the network trace doesn't get lost in the noise.
# ---------------------------------------------------------------------------

run_logger = logging.getLogger("ani_test.run")
run_logger.setLevel(logging.INFO)
run_logger.addHandler(logging.StreamHandler(sys.stdout))
_run_file_handler = logging.FileHandler("ani_test.log", mode="w", encoding="utf-8")
_run_file_handler.setFormatter(logging.Formatter("%(message)s"))
run_logger.addHandler(_run_file_handler)
for h in run_logger.handlers:
    h.setFormatter(logging.Formatter("%(message)s"))

net_logger = logging.getLogger("ani_test.network")
net_logger.setLevel(logging.INFO)
_net_file_handler = logging.FileHandler(NETWORK_LOG_PATH, mode="w", encoding="utf-8")
_net_file_handler.setFormatter(logging.Formatter("%(message)s"))
net_logger.addHandler(_net_file_handler)


def _truncate(body: str) -> str:
    if len(body) <= LOG_BODY_LIMIT:
        return body
    return body[:LOG_BODY_LIMIT] + f"\n... [truncated, {len(body)} chars total — see ani_test_output/ for full saves of key responses]"


async def _log_request(request: httpx.Request):
    request._ani_test_start = time.monotonic()
    body = ""
    if request.content:
        try:
            body = request.content.decode("utf-8", errors="replace")
        except Exception:
            body = f"<{len(request.content)} bytes, undecodable>"
    net_logger.info(
        "\n" + "=" * 100 +
        f"\n>>> REQUEST  {request.method} {request.url}"
        f"\nHeaders: {dict(request.headers)}"
        + (f"\nBody: {_truncate(body)}" if body else "")
    )


async def _log_response(response: httpx.Response):
    await response.aread()  # ensure body is available for logging without consuming it for the caller
    elapsed = time.monotonic() - getattr(response.request, "_ani_test_start", time.monotonic())
    try:
        body_text = response.text
    except Exception:
        body_text = f"<{len(response.content)} bytes, non-text>"
    net_logger.info(
        f"<<< RESPONSE {response.status_code} {response.request.url}  ({elapsed*1000:.0f}ms)"
        f"\nHeaders: {dict(response.headers)}"
        f"\nBody: {_truncate(body_text)}"
        + "\n" + "=" * 100
    )


def make_logging_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20.0,
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )


async def dump_raw_languages(provider: AniDBProvider, internal_ep_id: str, label: str) -> dict:
    """Hits /languages directly (bypassing the eng/jpn-only filter in extract_stream)
    so we can see EVERY language code anidb.app actually has for this episode."""
    url = f"{provider.BASE_URL}/api/frontend/episode/{internal_ep_id}/languages"
    resp = await provider.client.get(url, headers=provider.HEADERS)
    OUTPUT_DIR.mkdir(exist_ok=True)
    raw_path = OUTPUT_DIR / f"languages_{label}_{internal_ep_id}.json"
    raw_path.write_text(resp.text, encoding="utf-8")
    run_logger.info(f"    Saved full /languages response -> {raw_path}")

    if resp.status_code != 200:
        run_logger.info(f"    /languages returned HTTP {resp.status_code} — see network log for body")
        return {}

    data = resp.json()
    langs = data.get("languages", [])
    run_logger.info(f"    ALL language codes anidb.app has for this episode: {[l.get('code') for l in langs]}")
    for l in langs:
        run_logger.info(f"      code={l.get('code')!r}  embed_url={l.get('embed_url')}")
    return data


async def dump_embed_and_check_subs(provider: AniDBProvider, embed_url: str, label: str):
    """Fetches the raw embed page and saves it, then does a best-effort scan
    for anything that looks like a subtitle/track array alongside the video
    source, so we can tell whether subtitles exist upstream at all versus
    just not being extracted by the current code."""
    resp = await provider.client.get(embed_url, headers=provider.HEADERS)
    OUTPUT_DIR.mkdir(exist_ok=True)
    safe_label = label.replace("/", "_")
    raw_path = OUTPUT_DIR / f"embed_{safe_label}.html"
    raw_path.write_text(resp.text, encoding="utf-8")
    run_logger.info(f"    Saved full embed HTML -> {raw_path} ({len(resp.text)} chars, HTTP {resp.status_code})")

    html = resp.text
    hints = ["tracks:", "\"tracks\"", "'tracks'", "captions", "subtitle", "vtt", "srt"]
    found = [h for h in hints if h.lower() in html.lower()]
    if found:
        run_logger.info(f"    Embed HTML contains possible subtitle indicators: {found} — inspect {raw_path} to confirm the exact shape")
    else:
        run_logger.info(f"    No subtitle-related keywords found in embed HTML at all — this embed may genuinely have no subtitles for this language/episode")


async def run_episode_test(provider: AniDBProvider, mapped_id: str, episode: int, dub_only: bool, sub_only: bool):
    run_logger.info(f"\n{'#'*100}\n# EPISODE {episode}  (mapped_id={mapped_id})\n{'#'*100}")

    context = None
    if MediaContext is not None:
        # Minimal context; get_source_offers can still resolve title internally without it,
        # this just mirrors what manager.py would normally hand the provider.
        context = None

    dub_values = []
    if not sub_only:
        dub_values.append(True)
    if not dub_only:
        dub_values.append(False)

    for is_dub in dub_values:
        label = "DUB" if is_dub else "SUB"
        run_logger.info(f"\n--- get_source_offers(is_dub={is_dub})  [{label}] ---")
        try:
            offers = await provider.get_source_offers(mapped_id, episode, is_dub=is_dub, context=context)
        except Exception as e:
            run_logger.info(f"    EXCEPTION during get_source_offers: {e}")
            continue

        if not offers:
            run_logger.info(f"    No offer returned for {label} — AniDB either couldn't match a title on anidb.app, "
                             f"or this episode index is out of range. Check ani_test_network.log around the "
                             f"/browse?q=... and /episodes calls above for why.")
            continue

        offer = offers[0]
        run_logger.info(f"    Offer OK: provider={offer.provider} dub={offer.dub} payload_url={offer.url}")

        # Pull the internal AniDB episode id back out of the cache the provider just populated,
        # so we can inspect the raw languages endpoint directly.
        best_match = provider._mapping_cache.get(mapped_id)
        episodes = provider._episodes_cache.get(best_match, []) if best_match else []
        target_idx = episode - 1
        internal_ep_id = str(episodes[target_idx].get("id")) if 0 <= target_idx < len(episodes) else None

        if internal_ep_id:
            run_logger.info(f"  Checking RAW /languages for internal_ep_id={internal_ep_id} (anidb.app show id={best_match}):")
            lang_data = await dump_raw_languages(provider, internal_ep_id, label=f"{label}_{mapped_id}")

            for lang in lang_data.get("languages", []):
                code = lang.get("code")
                embed_url = lang.get("embed_url")
                if embed_url:
                    run_logger.info(f"  Fetching embed for code={code!r}:")
                    await dump_embed_and_check_subs(provider, embed_url, label=f"{mapped_id}_ep{episode}_{code}")

        # Now actually run extract_stream, exactly as the real player would via /api/player/payload
        run_logger.info(f"\n--- extract_stream() for {label} ---")
        try:
            from app.providers.base import encode_ep_id
            payload_id = encode_ep_id({"ep_id": internal_ep_id, "is_dub": is_dub}) if internal_ep_id else None
            if not payload_id:
                run_logger.info("    Skipping extract_stream — no internal_ep_id resolved above.")
                continue
            state = await provider.extract_stream(payload_id)
            run_logger.info(f"    stream_url = {state.stream_url}")
            run_logger.info(f"    tracks ({len(state.tracks)}):")
            for t in state.tracks:
                run_logger.info(f"      - label={t.label!r} kind={t.kind} default={t.default} file={t.file}")
            if not state.tracks:
                run_logger.info("      (none — see the subtitle-indicator check above for whether the embed even has any)")
        except Exception as e:
            run_logger.info(f"    EXCEPTION during extract_stream: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Diagnose AniDB sub/dub + subtitle availability")
    parser.add_argument("--anilist-id", type=int, help="AniList numeric id (mapped_id will be a{id})")
    parser.add_argument("--mal-id", type=int, help="MyAnimeList numeric id (mapped_id will be m{id})")
    parser.add_argument("--episode", type=int, action="append", required=True,
                         help="Absolute episode number to test. Repeat flag for multiple episodes.")
    parser.add_argument("--dub-only", action="store_true", help="Only test DUB (is_dub=True)")
    parser.add_argument("--sub-only", action="store_true", help="Only test SUB (is_dub=False)")
    args = parser.parse_args()

    if not args.anilist_id and not args.mal_id:
        parser.error("Provide --anilist-id or --mal-id")
    if args.dub_only and args.sub_only:
        parser.error("--dub-only and --sub-only are mutually exclusive")

    mapped_id = f"a{args.anilist_id}" if args.anilist_id else f"m{args.mal_id}"

    run_logger.info(f"AniDB diagnostic run — mapped_id={mapped_id}  episodes={args.episode}")
    run_logger.info(f"Network trace -> {NETWORK_LOG_PATH.resolve()}")
    run_logger.info(f"Saved raw responses -> {OUTPUT_DIR.resolve()}\n")

    client = make_logging_client()
    provider = AniDBProvider(client=client)

    try:
        for ep in args.episode:
            await run_episode_test(provider, mapped_id, ep, dub_only=args.dub_only, sub_only=args.sub_only)
    finally:
        await provider.close()

    run_logger.info(f"\nDone. Check:\n  - ani_test.log             (this run's narrative)\n"
                     f"  - ani_test_network.log     (every raw HTTP request/response)\n"
                     f"  - ani_test_output/         (full /languages JSON + embed HTML per language)")


if __name__ == "__main__":
    asyncio.run(main())