import re
import httpx
import urllib.parse
from typing import List, Dict

from app.providers.base import BaseProvider
from app.models.player import SourceOffer, SourceType, PlayerStateModel, Track
from app.services import player_proxy as pp
from app.providers.metadata.anilist import get_anilist_details


def levenshtein_distance(s1: str, s2: str) -> float:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
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
    return 1 - (distance / max_len)


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
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://anidb.app",
        "Accept": "application/json, text/plain, */*",
    }

    async def get_source_offers(self, mapped_id: str, episode_absolute: int, is_dub: bool = False) -> List[SourceOffer]:
        raw_id_str = ''.join([c for c in mapped_id if c.isdigit()])
        if not raw_id_str:
            return []
            
        raw_id = int(raw_id_str)
        is_mal = mapped_id.startswith("m") or "mal" in mapped_id
        title = None

        async with httpx.AsyncClient(headers=self.HEADERS, timeout=10.0) as client:
            try:
                if is_mal:
                    resp = await client.get(f"{self.JIKAN_API}/{raw_id}")
                    if resp.status_code == 200:
                        data = resp.json().get('data', {})
                        title = data.get('title_english') or data.get('title')
                else:
                    # AniList ID
                    anilist_data = await get_anilist_details(anilist_id=raw_id)
                    if anilist_data:
                        title_dict = anilist_data.get('title', {})
                        title = title_dict.get('english') or title_dict.get('romaji') or title_dict.get('native')

                if not title:
                    return []
                
                search_url = f"{self.BASE_URL}/browse?q={urllib.parse.quote(title)}"
                search_resp = await client.get(search_url)
                matches = re.finditer(r'<a href="(https?://anidb\.app/anime/[^"]+)" class="anime-card[^"]*" title="([^"]+)"', search_resp.text)
                
                best_match = None
                best_score = -1
                target_norm = normalize_title(title)
                
                for m in matches:
                    url = m.group(1)
                    t = m.group(2).strip()
                    id_match = re.search(r'-(\d+)$', url)
                    if not id_match:
                        continue
                    
                    score = 1 - levenshtein_distance(normalize_title(t), target_norm)
                    if score > best_score and (target_norm in normalize_title(t) or score > 0.6):
                        best_score = score
                        best_match = id_match.group(1)
                        
                if not best_match:
                    return []

                ep_resp = await client.get(f"{self.BASE_URL}/api/frontend/anime/{best_match}/episodes")
                if ep_resp.status_code != 200:
                    return []
                
                episodes = ep_resp.json().get("episodes", [])
                target_idx = episode_absolute - 1
                if target_idx < 0 or target_idx >= len(episodes):
                    return []
                
                internal_ep_id = str(episodes[target_idx].get("id"))
                payload_id = f"{internal_ep_id}_{'dub' if is_dub else 'sub'}"
                
                external_player_url = (
                    f"/player/stream?provider={self.name}&ep_id={payload_id}"
                    f"&title={urllib.parse.quote(title)}&episodeId={episode_absolute}"
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
        parts = provider_ep_id.split("_")
        internal_ep_id = parts[0]
        is_dub = (parts[1] == "dub") if len(parts) > 1 else False
        lang_code = "eng" if is_dub else "jpn"
        
        async with httpx.AsyncClient(headers=self.HEADERS, timeout=15.0) as client:
            lang_resp = await client.get(f"{self.BASE_URL}/api/frontend/episode/{internal_ep_id}/languages")
            if lang_resp.status_code != 200:
                raise Exception("Failed to fetch languages")
            
            embed_url = None
            for lang in lang_resp.json().get("languages", []):
                if lang.get("code") == lang_code:
                    embed_url = lang.get("embed_url")
                    break
                    
            if not embed_url:
                raise Exception(f"Language {lang_code} not found")
            
            embed_resp = await client.get(embed_url)
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

            # Tokenize & register stream with host header overrides to bypass CORS/hotlinking
            normalized = pp._normalize_stream_url(raw_stream_url)
            host = pp._normalize_host(normalized)
            pp._save_host_header_override(
                host=host,
                referer="https://anidb.app/",
                origin="https://anidb.app",
                user_agent=pp.USER_AGENT
            )
            token = pp.register_stream(normalized)
            proxy_stream_url = f"/hls/{token}/index.m3u8"

            return PlayerStateModel(
                stream_url=proxy_stream_url,
                stream_type="hls",
                headers={"Referer": self.BASE_URL},
                tracks=[],
                skips=[],
                media_title="AniDB Stream",
                episode_title=f"Episode {internal_ep_id}",
                provider_used=self.name
            )