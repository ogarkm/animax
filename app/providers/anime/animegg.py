import re
import json
import urllib.parse
from typing import List, Dict, Optional
import httpx

# Advanced Cloudflare Bypass / Session Handling
try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from app.providers.base import BaseProvider, encode_ep_id, decode_ep_id
from app.models.player import SourceOffer, SourceType, PlayerStateModel, Track, MediaContext
from app.services import player_proxy as pp
from app.providers.metadata.anilist import get_anilist_details


def levenshtein_similarity(s1: str, s2: str) -> float:
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    if len(s1) < len(s2):
        return levenshtein_similarity(s2, s1)
    
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
        
    distance = previous_row[-1]
    max_len = max(len(s1), len(s2))
    return 1.0 - (distance / max_len)


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r'(season|cour|part|the animation|the movie|movie)', '', t)
    t = re.sub(r'\d+(st|nd|rd|th)', lambda m: m.group(0).replace(m.group(1), ''), t)
    t = re.sub(r'[^a-z0-9]+', '', t)
    t = re.sub(r'(?<!i)ii(?!i)', '2', t)
    return t


class AnimeGGProvider(BaseProvider):
    name = "animegg"
    provider_type = "anime"
    
    BASE_URL = "https://www.animegg.org"
    JIKAN_API = "https://api.jikan.moe/v4/anime"
    
    BASE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.animegg.org/",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__(client=client)
        self._mapping_cache: Dict[str, str] = {}
        self._episodes_cache: Dict[str, List[dict]] = {}

    async def _fetch_html(self, url: str, referer: Optional[str] = None) -> str:
        headers = self.BASE_HEADERS.copy()
        if referer:
            headers["Referer"] = referer

        # Route through residential tunnel if connected
        ws_connected = pp.tunnel_websocket is not None
        poll_connected = pp.poll_tunnel_last_seen is not None and (pp.time.time() - pp.poll_tunnel_last_seen) <= pp.TUNNEL_CLIENT_TIMEOUT
        
        if (ws_connected or poll_connected) and not pp.NO_TUNNEL:
            try:
                resp = await pp._request_via_tunnel("GET", url, headers=headers)
                return resp.text
            except Exception as e:
                print(f"[AnimeGG] Tunnel request failed, falling back: {e}")

        if HAS_CURL_CFFI:
            try:
                async with AsyncSession(impersonate="chrome") as session:
                    resp = await session.get(url, headers=headers)
                    return resp.text
            except Exception as e:
                print(f"[AnimeGG] curl_cffi request failed, falling back to httpx: {e}")

        resp = await self.client.get(url, headers=headers, follow_redirects=True)
        return resp.text

    async def get_source_offers(
        self, 
        mapped_id: str, 
        episode_absolute: int, 
        is_dub: bool = False,
        context: Optional[MediaContext] = None
    ) -> List[SourceOffer]:
        raw_id_str = ''.join([c for c in mapped_id if c.isdigit()])
        if not raw_id_str:
            return []
            
        raw_id = int(raw_id_str)
        is_mal = mapped_id.startswith("m") or "mal" in mapped_id
        is_dub = bool(is_dub)

        try:
            cache_key = f"{mapped_id}_{'dub' if is_dub else 'sub'}"
            series_slug = self._mapping_cache.get(cache_key)
            title = None

            if not series_slug:
                base_candidates = []
                if context:
                    if context.english_title:
                        base_candidates.append(context.english_title)
                    if context.primary_title and context.primary_title not in base_candidates:
                        base_candidates.append(context.primary_title)
                    if context.romaji_title and context.romaji_title not in base_candidates:
                        base_candidates.append(context.romaji_title)
                    for syn in context.synonyms:
                        if syn and syn not in base_candidates:
                            base_candidates.append(syn)
                else:
                    if is_mal:
                        try:
                            resp = await self.client.get(f"{self.JIKAN_API}/{raw_id}")
                            if resp.status_code == 200:
                                data = resp.json().get('data', {})
                                title = data.get('title_english') or data.get('title')
                        except Exception:
                            pass
                    else:
                        anilist_data = await get_anilist_details(anilist_id=raw_id)
                        if anilist_data:
                            title_dict = anilist_data.get('title', {})
                            title = title_dict.get('english') or title_dict.get('romaji') or title_dict.get('native')
                    if title:
                        base_candidates.append(title)

                if not base_candidates:
                    return []

                # Build search queries prioritizing (Dub) if dub requested
                candidate_titles = []
                if is_dub:
                    for b in base_candidates:
                        candidate_titles.append(f"{b} (Dub)")
                        candidate_titles.append(f"{b} Dubbed")
                    for b in base_candidates:
                        candidate_titles.append(b)
                else:
                    candidate_titles = base_candidates

                best_score = -1.0
                current_best_slug = None

                for query in candidate_titles:
                    search_url = f"{self.BASE_URL}/search/?q={urllib.parse.quote(query)}"
                    search_html = await self._fetch_html(search_url)

                    # Matches: <a href="/series/..." class="mse">...<h2>Title</h2>
                    matches = re.findall(r'<a href="(/series/[^"]+)" class="mse">.*?<h2>(.*?)</h2>', search_html, re.DOTALL | re.IGNORECASE)
                    if not matches:
                        matches = re.findall(r'<a href="(/series/[^"]+)"[^>]*>.*?<h2>(.*?)</h2>', search_html, re.DOTALL | re.IGNORECASE)

                    target_norm = normalize_title(query)

                    for relative_url, matched_title in matches:
                        slug = relative_url.replace("/series/", "").strip("/")
                        matched_title_clean = matched_title.strip()
                        norm_t = normalize_title(matched_title_clean)
                        t_is_dub = "dub" in matched_title_clean.lower() or "dub" in slug.lower()

                        if is_dub and "(dub)" in query.lower() and not t_is_dub:
                            continue
                        if not is_dub and t_is_dub:
                            continue

                        if norm_t == target_norm:
                            series_slug = slug
                            break

                        sim = levenshtein_similarity(norm_t, target_norm)
                        if any(k in norm_t for k in ["special", "ova", "movie"]) and not any(k in target_norm for k in ["special", "ova", "movie"]):
                            sim *= 0.4

                        if sim > best_score and sim >= 0.55:
                            best_score = sim
                            current_best_slug = slug

                    if series_slug:
                        break
                    elif current_best_slug and best_score >= 0.75:
                        series_slug = current_best_slug
                        break

                if not series_slug and current_best_slug:
                    series_slug = current_best_slug

                if not series_slug:
                    return []

                self._mapping_cache[cache_key] = series_slug

            # 2. Episode Listing Layer
            if series_slug in self._episodes_cache:
                episodes = self._episodes_cache[series_slug]
            else:
                series_url = f"{self.BASE_URL}/series/{series_slug}"
                series_html = await self._fetch_html(series_url)

                # Match episode links: <a href="..." class="anm_det_pop">...<strong>Episode X</strong>...<i class="anititle">Title</i>
                ep_regex = re.compile(
                    r'<a href="([^"]+)" class="anm_det_pop">[\s\S]*?<strong>(.*?)</strong>[\s\S]*?<i class="anititle">(.*?)</i>',
                    re.IGNORECASE
                )
                ep_matches = ep_regex.findall(series_html)
                if not ep_matches:
                    # Fallback pattern for episode items
                    ep_regex_alt = re.compile(
                        r'<a href="([^"]+)"[^>]*class="[^"]*anm_det_pop[^"]*"[\s\S]*?<strong>(.*?)</strong>',
                        re.IGNORECASE
                    )
                    ep_matches_alt = ep_regex_alt.findall(series_html)
                    ep_matches = [(href, strong, "") for href, strong in ep_matches_alt]

                episodes = []
                for href, strong_text, italic_text in ep_matches:
                    ep_num_match = re.search(r'-episode-(\d+)', href)
                    if ep_num_match:
                        ep_num = int(ep_num_match.group(1))
                    else:
                        num_match = re.search(r'(\d+)$', strong_text.strip())
                        ep_num = int(num_match.group(1)) if num_match else 0

                    episodes.append({
                        "href": href,
                        "title": italic_text.strip() if italic_text else f"Episode {ep_num}",
                        "number": ep_num,
                        "strong": strong_text.strip()
                    })

                if episodes:
                    self._episodes_cache[series_slug] = episodes

            if not episodes:
                return []

            # Locate target episode
            target_ep = next((ep for ep in episodes if ep["number"] == episode_absolute), None)
            if not target_ep:
                target_idx = episode_absolute - 1
                if 0 <= target_idx < len(episodes):
                    target_ep = episodes[target_idx]
                else:
                    return []

            display_title = (context.english_title or context.primary_title) if context else (title or mapped_id)

            payload_data = {
                "ep_href": target_ep["href"],
                "is_dub": is_dub,
                "media_title": display_title,
                "episode_num": episode_absolute,
                "ep_title": target_ep.get("title") or f"Episode {episode_absolute}"
            }
            payload_id = encode_ep_id(payload_data)

            external_player_url = (
                f"/player/stream?provider={self.name}&ep_id={payload_id}"
                f"&title={urllib.parse.quote(display_title)}&episodeId={episode_absolute}"
                f"&media_id={urllib.parse.quote(mapped_id)}"
            )

            return [
                SourceOffer(
                    provider=self.name,
                    type=SourceType.INTERNAL,
                    quality="auto",
                    dub=is_dub,
                    url=f"/api/player/payload?provider={self.name}&ep_id={payload_id}",
                    external_player_url=external_player_url
                )
            ]
        except Exception as e:
            print(f"[AnimeGG] Offer Error: {e}")
            return []

    async def extract_stream(self, provider_ep_id: str) -> PlayerStateModel:
        decoded = decode_ep_id(provider_ep_id)
        if "ep_href" in decoded:
            ep_href = str(decoded["ep_href"])
            is_dub = bool(decoded.get("is_dub", False))
            media_title = decoded.get("media_title") or "AnimeGG Stream"
            episode_num = decoded.get("episode_num")
            ep_title = decoded.get("ep_title")
        else:
            raw = decoded.get("raw", provider_ep_id)
            ep_href = raw
            is_dub = False
            media_title = "AnimeGG Stream"
            episode_num = None
            ep_title = None

        ep_url = f"{self.BASE_URL}{ep_href}" if ep_href.startswith("/") else ep_href
        ep_html = await self._fetch_html(ep_url, referer=self.BASE_URL)

        target_tab_id = "dubbed-Animegg" if is_dub else "subbed-Animegg"

        # Tab fallback if requested server is not available
        if f'id="{target_tab_id}"' not in ep_html:
            if target_tab_id == "subbed-Animegg" and 'id="dubbed-Animegg"' in ep_html:
                target_tab_id = "dubbed-Animegg"
            elif target_tab_id == "dubbed-Animegg" and 'id="subbed-Animegg"' in ep_html:
                target_tab_id = "subbed-Animegg"

        tab_regex = re.compile(rf'<div id="{target_tab_id}"[^>]*>\s*<iframe\s+src="([^"]+)"', re.DOTALL | re.IGNORECASE)
        iframe_match = tab_regex.search(ep_html)

        if not iframe_match:
            # Fallback iframe search
            iframe_match = re.search(r'<iframe[^>]*src="(/embed/[^"]+)"', ep_html, re.IGNORECASE)
            if not iframe_match:
                iframe_match = re.search(r'<iframe[^>]*src="([^"]+)"', ep_html, re.IGNORECASE)

        if not iframe_match:
            raise Exception("Embed iframe not found on AnimeGG episode page")

        iframe_src = iframe_match.group(1)
        embed_url = f"{self.BASE_URL}{iframe_src}" if iframe_src.startswith("/") else iframe_src

        embed_html = await self._fetch_html(embed_url, referer=ep_url)

        source_match = re.search(r'var\s+videoSources\s*=\s*(\[.*?\])', embed_html, re.DOTALL)
        if not source_match:
            # Fallback: search for file / video source attributes
            source_match = re.search(r'videoSources\s*=\s*(\[.*?\])', embed_html, re.DOTALL)

        if not source_match:
            raise Exception("Video sources variable not found in embed")

        raw_source_str = source_match.group(1)
        parsed_sources = []

        obj_regex = re.compile(r'\{\s*file:\s*["\']([^"\']+)["\']\s*,\s*label:\s*["\']([^"\']+)["\']', re.DOTALL | re.IGNORECASE)
        for m in obj_regex.finditer(raw_source_str):
            parsed_sources.append({"file": m.group(1), "label": m.group(2)})

        if not parsed_sources:
            # Try alternate JSON / loose parsing
            alt_regex = re.compile(r'["\']?file["\']?\s*:\s*["\']([^"\']+)["\'].*?["\']?label["\']?\s*:\s*["\']([^"\']+)["\']', re.DOTALL | re.IGNORECASE)
            for m in alt_regex.finditer(raw_source_str):
                parsed_sources.append({"file": m.group(1), "label": m.group(2)})

        if not parsed_sources:
            raise Exception("No video sources parsed from AnimeGG embed")

        def parse_quality(label: str) -> int:
            m = re.search(r'\d+', str(label))
            return int(m.group(0)) if m else 0

        best_source = max(parsed_sources, key=lambda s: parse_quality(s.get("label", "0")))
        raw_video_file = best_source.get("file", "")
        raw_video_url = f"{self.BASE_URL}{raw_video_file}" if raw_video_file.startswith("/") else raw_video_file

        # Register host headers & proxy stream URL for CORS bypass
        normalized = pp._normalize_stream_url(raw_video_url)
        host = pp._normalize_host(normalized)
        pp._save_host_header_override(
            host=host,
            referer=self.BASE_URL,
            origin=self.BASE_URL,
            user_agent=self.BASE_HEADERS["User-Agent"]
        )

        proxy_stream_url = f"/proxy?url={urllib.parse.quote(normalized)}"

        episode_title = ep_title or (f"Episode {episode_num}" if episode_num else "Episode")

        return PlayerStateModel(
            stream_url=proxy_stream_url,
            stream_type="mp4",
            headers={"Referer": self.BASE_URL},
            tracks=[],
            skips=[],
            media_title=media_title,
            episode_title=episode_title,
            provider_used=self.name
        )
