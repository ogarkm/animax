import httpx
from app.core.config import settings
from app.models.media import BaseMediaCard, MediaType
import asyncio
from typing import Optional
from app.models.media import EpisodeShort

TMDB_BASE_URL = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w500"

async def fetch_tmdb_trending(media_type: str = "movie") -> list:
    """Fetches trending movies or tv shows and formats to Unified Model."""
    url = f"{TMDB_BASE_URL}/trending/{media_type}/day"
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"api_key": settings.TMDB_API_KEY})
        if resp.status_code != 200:
            return []
            
        results = resp.json().get("results", [])
        
        formatted_list = []
        for item in results:
            # Map TMDB IDs to our custom prefixes
            prefix = "tm" if media_type == "movie" else "tt"
            custom_id = f"{prefix}{item.get('id')}"
            
            title = item.get("title") or item.get("name")
            release_date = item.get("release_date") or item.get("first_air_date") or ""
            year = int(release_date.split("-")[0]) if release_date else None
            
            card = BaseMediaCard(
                id=custom_id,
                title=title,
                type=MediaType.MOVIE if media_type == "movie" else MediaType.TV,
                poster_url=f"{IMG_BASE}{item.get('poster_path')}" if item.get('poster_path') else None,
                banner_url=f"https://image.tmdb.org/t/p/w1280{item.get('backdrop_path')}" if item.get('backdrop_path') else None,
                release_year=year,
                rating=round(item.get("vote_average", 0), 1)
            )
            formatted_list.append(card.model_dump()) # Store as dict for caching
            
        return formatted_list
    
async def search_tmdb(query: str) -> list:
    """Searches both movies and TV shows concurrently."""
    url = f"{TMDB_BASE_URL}/search/multi"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"api_key": settings.TMDB_API_KEY, "query": query})
        if resp.status_code != 200:
            return []
            
        formatted_list = []
        for item in resp.json().get("results", []):
            m_type = item.get("media_type")
            if m_type not in ["movie", "tv"]:
                continue # Skip actors/people
                
            prefix = "tm" if m_type == "movie" else "tt"
            release_date = item.get("release_date") or item.get("first_air_date") or ""
            
            card = BaseMediaCard(
                id=f"{prefix}{item.get('id')}",
                title=item.get("title") or item.get("name"),
                type=MediaType.MOVIE if m_type == "movie" else MediaType.TV,
                poster_url=f"{IMG_BASE}{item.get('poster_path')}" if item.get('poster_path') else None,
                release_year=int(release_date.split("-")[0]) if release_date else None,
                rating=round(item.get("vote_average", 0), 1)
            )
            formatted_list.append(card.model_dump())
        return formatted_list

async def get_tmdb_details(tmdb_id: int, is_tv: bool) -> Optional[dict]:
    media_type = "tv" if is_tv else "movie"
    
    # Bundle credits, recommendations, similar, and age ratings into ONE request
    append_str = "videos,images,credits,recommendations,similar"
    append_str += ",content_ratings" if is_tv else ",release_dates"
    
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
    params = {
        "api_key": settings.TMDB_API_KEY, 
        "language": "en-US",
        "append_to_response": append_str
    }
    
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        return resp.json()
    
async def get_tmdb_seasons_and_episodes(tv_id: int, default_seasons: list) -> tuple[list, list]:
    """Checks for TMDB Episode Groups, else falls back to default seasons with Season 0 pushed to the end."""
    seasons_data = []
    episodes = []
    absolute_counter = 1

    async with httpx.AsyncClient() as client:
        # 1. Check for Episode Groups (For Anime like Solo Leveling)
        group_url = f"{TMDB_BASE_URL}/tv/{tv_id}/episode_groups"
        resp = await client.get(group_url, params={"api_key": settings.TMDB_API_KEY})
        group_data = []
        
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            target_group = None
            for g in results:
                if g.get("type") in [5, 7] or "anime" in g.get("name", "").lower():
                    target_group = g.get("id")
                    break
            if not target_group and results and results[0].get("group_count", 0) > 1:
                target_group = results[0].get("id")
                
            if target_group:
                g_resp = await client.get(f"{TMDB_BASE_URL}/tv/episode_group/{target_group}", params={"api_key": settings.TMDB_API_KEY})
                if g_resp.status_code == 200:
                    group_data = g_resp.json().get("groups", [])

        # 2. Parse the Data
        if group_data:
            for idx, g in enumerate(group_data):
                s_num = idx + 1
                eps = g.get("episodes", [])
                if not eps: continue
                
                seasons_data.append({
                    "season_number": s_num, "title": g.get("name"), "episode_count": len(eps),
                    "poster_url": None, "original_season": eps[0].get("season_number")
                })
                
                for ep_idx, ep in enumerate(eps):
                    img = ep.get("still_path")
                    episodes.append(EpisodeShort(
                        id=f"tt{tv_id}_s{s_num}_e{ep_idx+1}", mapped_id="", absolute_number=absolute_counter,
                        season_number=s_num, episode_number=ep_idx + 1, title=ep.get("name"),
                        thumbnail_url=f"{IMG_BASE}{img}" if img else None, air_date=ep.get("air_date"), synopsis=ep.get("overview")
                    ))
                    absolute_counter += 1
        else:
            # Standard TV Show Fallback
            # FILTER: Get seasons with episodes
            unfiltered_seasons = [s for s in default_seasons if s["episode_count"] > 0]
            
            # SORT MAGIC: Pushes Season 0 (Specials) to the absolute end of the array, 
            # ensuring Season 1 Episode 1 gets absolute_number = 1!
            valid_seasons = sorted(
                unfiltered_seasons,
                key=lambda x: (x["season_number"] == 0, x["season_number"])
            )
            
            tasks = [client.get(f"{TMDB_BASE_URL}/tv/{tv_id}/season/{s['season_number']}", params={"api_key": settings.TMDB_API_KEY}) for s in valid_seasons]
            season_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for s, s_resp in zip(valid_seasons, season_results):
                if isinstance(s_resp, Exception) or s_resp.status_code != 200: continue
                s_data = s_resp.json()
                
                seasons_data.append({
                    "season_number": s["season_number"], "title": s["name"], "episode_count": s["episode_count"],
                    "poster_url": f"{IMG_BASE}{s.get('poster_path')}" if s.get('poster_path') else None, "original_season": s["season_number"]
                })
                
                for ep in s_data.get("episodes", []):
                    img = ep.get("still_path")
                    episodes.append(EpisodeShort(
                        id=f"tt{tv_id}_s{s['season_number']}_e{ep.get('episode_number')}", mapped_id="", absolute_number=absolute_counter,
                        season_number=s['season_number'], episode_number=ep.get("episode_number"), title=ep.get("name"),
                        thumbnail_url=f"{IMG_BASE}{img}" if img else None, air_date=ep.get("air_date"), synopsis=ep.get("overview")
                    ))
                    absolute_counter += 1

    return seasons_data, episodes