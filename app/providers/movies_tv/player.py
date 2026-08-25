import urllib.parse
from typing import List, Optional

from app.core.config import settings
from app.providers.base import BaseProvider, encode_ep_id, decode_ep_id
from app.models.player import SourceOffer, SourceType, PlayerStateModel, Track, MediaContext
from app.providers.metadata.tmdb import get_tmdb_details, get_tmdb_seasons_and_episodes, IMG_BASE
from app.services.player_proxy import fetch_and_decrypt_stream

class VideasyProvider(BaseProvider):
    name = "videasy"
    provider_type = "movie_tv"

    async def get_source_offers(
        self, 
        mapped_id: str, 
        episode_absolute: int, 
        is_dub: bool = False,
        context: Optional[MediaContext] = None
    ) -> List[SourceOffer]:
        """
        Calculates TMDB season/episode indexes, extracts metadata, 
        and constructs the unified player URL.
        """
        prefix = ''.join([c for c in mapped_id if not c.isdigit()])
        raw_id_str = ''.join([c for c in mapped_id if c.isdigit()])
        
        raw_id = int(raw_id_str) if raw_id_str else None
        is_tv = (prefix == "tt")
        
        if prefix not in ["tt", "tm"] or not raw_id:
            if context and context.tmdb_tv_id:
                raw_id = context.tmdb_tv_id
                is_tv = True
            elif context and context.tmdb_movie_id:
                raw_id = context.tmdb_movie_id
                is_tv = False
            else:
                return []
            
        payload_data = {"type": "tv" if is_tv else "movie", "tmdb_id": raw_id, "is_dub": is_dub}
        external_player_url = None
        
        try:
            # 1. Fetch Heavy TMDB Metadata
            data = await get_tmdb_details(raw_id, is_tv=is_tv)
            if not data: return []
            
            # 2. Extract Metadata for the Player UI
            title = context.primary_title if context else (data.get("name") if is_tv else data.get("title"))
            
            # Parse clear logo from TMDB images
            logos = data.get("images", {}).get("logos", [])
            best_logo = next((l for l in logos if l.get("iso_639_1") == "en"), logos[0] if logos else None)
            clear_logo_url = f"{IMG_BASE}{best_logo['file_path']}" if best_logo else None
            
            # Base synopsis fallback (Movie description or main TV Show description)
            synopsis = data.get("overview")
            
            # Prepare URL parameters dictionary
            player_params = {
                "title": title,
                "logo": clear_logo_url,
                "synopsis": synopsis,
                "year": context.year if (context and context.year) else (data.get("release_date") and data.get("release_date", "")[:4] or data.get("first_air_date") and data.get("first_air_date", "")[:4]),
                "imdbId": data.get("imdb_id"),
                # media_id lets the player UI resolve the episode/season drawer
                # via /api/player/episodes/{media_id} regardless of whether this
                # is a TV or movie TMDB id (raw_id alone is ambiguous between them).
                "media_id": mapped_id,
            }

            if is_tv:
                # TV Episode specific mapping
                _, episodes = await get_tmdb_seasons_and_episodes(raw_id, data.get("seasons", []))
                target_ep = next((ep for ep in episodes if ep.absolute_number == episode_absolute), None)
                if not target_ep: return []
                
                # Overwrite base synopsis with the specific EPISODE synopsis!
                if target_ep.synopsis:
                    player_params["synopsis"] = target_ep.synopsis
                    
                player_params["seasonId"] = target_ep.season_number
                player_params["episodeId"] = target_ep.episode_number
                
                payload_data["season"] = target_ep.season_number
                payload_data["episode"] = target_ep.episode_number

            payload_id = encode_ep_id(payload_data)

            # 3. Construct unified player URL
            clean_params = {k: str(v) for k, v in player_params.items() if v is not None}
            query_string = urllib.parse.urlencode(clean_params)
            
            external_player_url = f"/player/stream/{raw_id}?{query_string}"

        except Exception as e:
            print(f"[Videasy] Failed mapping/extracting player metadata: {e}")
            return []

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

    async def extract_stream(self, provider_ep_id: str) -> PlayerStateModel:
        """
        Directly executes stream decryption within the main application loop.
        """
        decoded = decode_ep_id(provider_ep_id)
        
        if "tmdb_id" in decoded:
            media_type = decoded.get("type", "movie")
            tmdb_id = str(decoded["tmdb_id"])
            season_val = decoded.get("season")
            episode_val = decoded.get("episode")
            is_dub = bool(decoded.get("is_dub", False))
        else:
            # Legacy string fallback (e.g. "movie_123" or "tv_123_s1_e1")
            raw = decoded.get("raw", provider_ep_id)
            parts = raw.split("_")
            media_type = parts[0]
            tmdb_id = parts[1]
            season_val = None
            episode_val = None
            is_dub = False
            if media_type == "tv" and len(parts) >= 4:
                season_str = parts[2].replace("s", "")
                episode_str = parts[3].replace("e", "")
                season_val = int(season_str) if season_str.isdigit() else 1
                episode_val = int(episode_str) if episode_str.isdigit() else 1

        payload_data = await fetch_and_decrypt_stream(
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season_val,
            episode=episode_val,
            is_dub=is_dub,
        )
            
        sources = payload_data.get("sources", [])
        if not sources:
            raise Exception("No active decrypted streams returned")

        # fetch_and_decrypt_stream sorts sources by language match when it can
        # tell them apart, but falls back to upstream order otherwise — so this
        # is a best-effort pick, not a guarantee, until the upstream response
        # shape is confirmed (see the language-detection comment there).
        selected_source = sources[0]
        stream_url = selected_source.get("url")
        
        tracks = []
        for idx, sub in enumerate(payload_data.get("subtitles", [])):
            tracks.append(Track(
                file=sub.get("url"),
                label=sub.get("language") or sub.get("lang") or f"Sub {idx+1}",
                kind="captions",
                default=idx == 0
            ))

        return PlayerStateModel(
            stream_url=stream_url,
            stream_type="hls",
            headers={"Referer": "https://player.videasy.to/"},
            tracks=tracks,
            skips=[],
            media_title="Videasy Playback",
            episode_title=f"{media_type.upper()} {tmdb_id}",
            provider_used=self.name
        )