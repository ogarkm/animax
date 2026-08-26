import re
import urllib.parse
from typing import List, Dict, Optional
import httpx

# Advanced Cloudflare Bypass
try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    print("[AniDB] WARNING: 'curl_cffi' is missing. Cloudflare will likely block requests. Run: pip install curl_cffi")

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


class AniDBProvider(BaseProvider):
    name = "anidb"
    provider_type = "anime"
    
    BASE_URL = "https://anidb.app"
    JIKAN_API = "https://api.jikan.moe/v4/anime"
    
    BASE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Referer": "https://anidb.app/",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
    }

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__(client=client)
        self._mapping_cache: Dict[str, str] = {}
        self._episodes_cache: Dict[str, List[dict]] = {}

    def _get_html_headers(self):
        h = self.BASE_HEADERS.copy()
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        h["sec-fetch-dest"] = "document"
        h["sec-fetch-mode"] = "navigate"
        return h
        
    def _get_api_headers(self):
        h = self.BASE_HEADERS.copy()
        h["Accept"] = "application/json, text/plain, */*"
        h["sec-fetch-dest"] = "empty"
        h["sec-fetch-mode"] = "cors"
        return h

    async def _cf_get(self, url: str, headers: dict):
        """Routes through the residential tunnel if connected; falls back to curl_cffi on local."""
        ws_connected = pp.tunnel_websocket is not None
        poll_connected = pp.poll_tunnel_last_seen is not None and (pp.time.time() - pp.poll_tunnel_last_seen) <= pp.TUNNEL_CLIENT_TIMEOUT
        
        if (ws_connected or poll_connected) and not pp.NO_TUNNEL:
            try:
                return await pp._request_via_tunnel("GET", url, headers=headers)
            except Exception as e:
                print(f"[AniDB] Tunnel request failed, falling back: {e}")

        if HAS_CURL_CFFI:
            async with AsyncSession(impersonate="chrome") as session:
                return await session.get(url, headers=headers)
        else:
            return await pp._request_via_tunnel("GET", url, headers=headers)

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
            best_match = self._mapping_cache.get(cache_key)
            is_explicit_dub_show = False
            title = None

            if not best_match:
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
                        resp = await self._cf_get(f"{self.JIKAN_API}/{raw_id}", headers=self._get_api_headers())
                        if resp.status_code == 200:
                            data = resp.json().get('data', {})
                            title = data.get('title_english') or data.get('title')
                    else:
                        anilist_data = await get_anilist_details(anilist_id=raw_id)
                        if anilist_data:
                            title_dict = anilist_data.get('title', {})
                            title = title_dict.get('english') or title_dict.get('romaji') or title_dict.get('native')
                    if title:
                        base_candidates.append(title)

                if not base_candidates:
                    return []

                # Build search queries: prioritize (Dub) suffix if dub requested
                candidate_titles = []
                if is_dub:
                    for b in base_candidates:
                        candidate_titles.append(f"{b} (Dub)")
                        candidate_titles.append(f"{b} (English Dub)")
                    for b in base_candidates:
                        candidate_titles.append(b)
                else:
                    candidate_titles = base_candidates

                for query in candidate_titles:
                    search_url = f"{self.BASE_URL}/browse?q={urllib.parse.quote(query)}"
                    search_resp = await self._cf_get(search_url, headers=self._get_html_headers())
                    
                    if search_resp.status_code != 200:
                        print(f"[AniDB] Search failed with status {search_resp.status_code} for '{query}'")
                        continue
                        
                    matches = list(re.finditer(r'<a href="(https?://anidb\.app/anime/[^"]+)" class="anime-card[^"]*" title="([^"]+)"', search_resp.text))
                    if not matches:
                        matches = list(re.finditer(r'<a href="(https?://anidb\.app/anime/[^"]+)"[^>]*title="([^"]+)"', search_resp.text))

                    target_norm = normalize_title(query)
                    best_score = -1.0
                    current_best_id = None
                    
                    for m in matches:
                        url = m.group(1)
                        t = m.group(2).strip()
                        id_match = re.search(r'-(\d+)$', url)
                        if not id_match:
                            continue
                        
                        anime_id = id_match.group(1)
                        norm_t = normalize_title(t)
                        t_is_dub = "dub" in t.lower()
                        
                        # Skip mismatched sub/dub titles if an explicit version was queried
                        if is_dub and "(dub)" in query.lower() and not t_is_dub:
                            continue
                        if not is_dub and t_is_dub:
                            continue

                        if norm_t == target_norm:
                            best_match = anime_id
                            if t_is_dub: is_explicit_dub_show = True
                            break
                        
                        sim = levenshtein_similarity(norm_t, target_norm)
                        
                        if any(k in norm_t for k in ["nomahou", "mini", "special", "ova", "chibi"]) and not any(k in target_norm for k in ["nomahou", "mini", "special", "ova", "chibi"]):
                            sim *= 0.4
                            
                        if sim > best_score and sim >= 0.60:
                            best_score = sim
                            current_best_id = anime_id
                            if t_is_dub: is_explicit_dub_show = True

                    if best_match:
                        break
                    elif current_best_id:
                        best_match = current_best_id
                        break

                if not best_match:
                    return []
                
                self._mapping_cache[cache_key] = best_match

            # 2. Episode Listing Layer
            if best_match in self._episodes_cache:
                episodes = self._episodes_cache[best_match]
            else:
                ep_url = f"{self.BASE_URL}/api/frontend/anime/{best_match}/episodes"
                ep_resp = await self._cf_get(ep_url, headers=self._get_api_headers())
                
                if ep_resp.status_code != 200:
                    print(f"[AniDB] Episodes fetch failed with status {ep_resp.status_code}")
                    return []
                    
                episodes = ep_resp.json().get("episodes", [])
                self._episodes_cache[best_match] = episodes

            target_idx = episode_absolute - 1
            if target_idx < 0 or target_idx >= len(episodes):
                return []
            
            internal_ep_id = str(episodes[target_idx].get("id"))

            # 3. Audio Track Verification: If dub requested, verify AniDB actually has a dub
            if is_dub and not is_explicit_dub_show:
                lang_url = f"{self.BASE_URL}/api/frontend/episode/{internal_ep_id}/languages"
                lang_resp = await self._cf_get(lang_url, headers=self._get_api_headers())
                if lang_resp.status_code == 200:
                    languages = lang_resp.json().get("languages", [])
                    has_dub_track = any(str(l.get("code", "")).lower() in ["eng", "en", "dub"] for l in languages)
                    if not has_dub_track:
                        # AniDB only has Sub for this anime; do not create a fake Dub offer!
                        return []

            display_title = (context.english_title or context.primary_title) if context else (title or mapped_id)

            payload_data = {
                "ep_id": internal_ep_id, 
                "is_dub": is_dub,
                "media_title": display_title,
                "episode_num": episode_absolute
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
            print(f"[AniDB] Offer Error: {e}")
            return []

    async def extract_stream(self, provider_ep_id: str) -> PlayerStateModel:
        decoded = decode_ep_id(provider_ep_id)
        if "ep_id" in decoded:
            internal_ep_id = str(decoded["ep_id"])
            is_dub = bool(decoded.get("is_dub", False))
            media_title = decoded.get("media_title") or "AniDB Stream"
            episode_num = decoded.get("episode_num")
        else:
            raw = decoded.get("raw", provider_ep_id)
            parts = raw.split("_")
            internal_ep_id = parts[0]
            is_dub = (parts[1] == "dub") if len(parts) > 1 else False
            media_title = "AniDB Stream"
            episode_num = None

        lang_code = "eng" if is_dub else "jpn"
        
        lang_url = f"{self.BASE_URL}/api/frontend/episode/{internal_ep_id}/languages"
        lang_resp = await self._cf_get(lang_url, headers=self._get_api_headers())
        
        if lang_resp.status_code != 200:
            raise Exception(f"Failed to fetch languages (Status: {lang_resp.status_code})")
        
        languages = lang_resp.json().get("languages", [])
        if not languages:
            raise Exception("No language streams found for this episode")

        embed_url = None
        target_codes = ["eng", "en", "dub"] if is_dub else ["jpn", "ja", "sub", "raw"]
        for lang in languages:
            code = str(lang.get("code", "")).lower()
            if code in target_codes:
                embed_url = lang.get("embed_url")
                break
                
        # Fallback to first available language if specific code isn't present
        if not embed_url:
            embed_url = languages[0].get("embed_url")
            
        if not embed_url:
            raise Exception("No embed URL found in languages response")
        
        embed_resp = await self._cf_get(embed_url, headers=self._get_html_headers())
        html_content = embed_resp.text
        
        regexes = [
            r"sources\s*:\s*\[\s*\{\s*file\s*:\s*'([^']+)'",
            r"file\s*:\s*'(https?://[^']+\.m3u8[^']*)'",
            r"[\"'](https?://[^\"']+\.m3u8[^\"']*)[\"']"
        ]
        
        raw_stream_url = None
        for pattern in regexes:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                raw_stream_url = match.group(1)
                break
                
        if not raw_stream_url:
            raise Exception("Failed to extract HLS stream from embed")

        subtitle_tracks: List[Track] = []
        
        # 1. Parse tracks from languages payload if provided
        for lang in languages:
            for sub in lang.get("subtitles", []):
                sub_url = sub.get("url") or sub.get("file")
                if sub_url:
                    subtitle_tracks.append(Track(
                        file=sub_url,
                        label=sub.get("label") or sub.get("language") or "Subtitles",
                        kind="captions",
                        default=True
                    ))

        # 2. Parse tracks embedded in the video player HTML
        tracks_block_match = re.search(r"tracks\s*:\s*\[(.*?)\]\s*[,}]", html_content, re.IGNORECASE | re.DOTALL)
        if tracks_block_match:
            tracks_raw = tracks_block_match.group(1)
            entry_regex = re.compile(
                r"\{\s*file\s*:\s*['\"]([^'\"]+)['\"][^}]*?"
                r"(?:label\s*:\s*['\"]([^'\"]*)['\"])?[^}]*?"
                r"(?:kind\s*:\s*['\"]([^'\"]*)['\"])?[^}]*?\}",
                re.IGNORECASE | re.DOTALL
            )
            for idx, m in enumerate(entry_regex.finditer(tracks_raw)):
                file_url, label, kind = m.group(1), m.group(2), m.group(3)
                if kind and kind.lower() not in ("captions", "subtitles", "subtitle"):
                    continue
                subtitle_tracks.append(Track(
                    file=file_url,
                    label=label or f"Track {idx + 1}",
                    kind="captions",
                    default=idx == 0
                ))

        normalized = pp._normalize_stream_url(raw_stream_url)
        host = pp._normalize_host(normalized)
        pp._save_host_header_override(
            host=host,
            referer="https://anidb.app/",
            origin="https://anidb.app",
            user_agent=self.BASE_HEADERS["User-Agent"]
        )
        token = pp.register_stream(normalized)
        proxy_stream_url = f"/hls/{token}/index.m3u8"

        episode_title = f"Episode {episode_num}" if episode_num else f"Episode {internal_ep_id}"

        return PlayerStateModel(
            stream_url=proxy_stream_url,
            stream_type="hls",
            headers={"Referer": self.BASE_URL},
            tracks=subtitle_tracks,
            skips=[],
            media_title=media_title,
            episode_title=episode_title,
            provider_used=self.name
        )