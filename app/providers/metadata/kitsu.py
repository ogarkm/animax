import httpx
from app.models.media import EpisodeShort

KITSU_BASE = "https://kitsu.io/api/edge"

async def get_kitsu_episodes(kitsu_id: int, mal_id: int, custom_prefix_id: str) -> list:
    """Fetches episodes using the EXACT kitsu_id from Fribb."""
    if not kitsu_id:
        return []
        
    async with httpx.AsyncClient() as client:
        episodes = []
        offset = 0
        limit = 20
        
        while True:
            ep_url = f"{KITSU_BASE}/anime/{kitsu_id}/episodes?page[limit]={limit}&page[offset]={offset}"
            ep_resp = await client.get(ep_url)
            if ep_resp.status_code != 200: break
            
            data = ep_resp.json().get("data", [])
            if not data: break
            
            for ep in data:
                attr = ep.get("attributes", {})
                ep_num = attr.get("relativeNumber") or attr.get("number")
                if not ep_num: continue
                
                thumb_dict = attr.get("thumbnail") or {}
                thumb = thumb_dict.get("original") or thumb_dict.get("large")
                
                episodes.append(
                    EpisodeShort(
                        id=f"{custom_prefix_id}_e{ep_num}",
                        mapped_id=f"m{mal_id}",
                        absolute_number=attr.get("number", ep_num),
                        season_number=1,
                        episode_number=ep_num,
                        title=attr.get("canonicalTitle") or f"Episode {ep_num}",
                        thumbnail_url=thumb,
                        synopsis=attr.get("synopsis"),
                        air_date=attr.get("airdate")
                    )
                )
            
            if len(data) < limit: break
            offset += limit
            
        return episodes
    
async def enrich_episodes_with_kitsu(episodes: list, kitsu_id: int):
    """Fetches Kitsu episodes and overwrites TMDB's thumbnails/synopsis."""
    if not kitsu_id: return
    
    async with httpx.AsyncClient() as client:
        offset = 0
        limit = 20
        kitsu_data_map = {}
        
        while True:
            url = f"{KITSU_BASE}/anime/{kitsu_id}/episodes?page[limit]={limit}&page[offset]={offset}"
            resp = await client.get(url)
            if resp.status_code != 200: break
            
            data = resp.json().get("data", [])
            if not data: break
            
            for ep in data:
                attr = ep.get("attributes", {})
                # relativeNumber is the episode number relative to the season
                ep_num = attr.get("relativeNumber") or attr.get("number")
                if not ep_num: continue
                
                thumb_dict = attr.get("thumbnail") or {}
                thumb = thumb_dict.get("original") or thumb_dict.get("large")
                
                kitsu_data_map[ep_num] = {
                    "synopsis": attr.get("synopsis"),
                    "thumbnail": thumb
                }
            if len(data) < limit: break
            offset += limit

        # Inject Kitsu data into the TMDB Episode models
        for ep in episodes:
            k_data = kitsu_data_map.get(ep.episode_number)
            if k_data:
                # Only overwrite if Kitsu actually returned valid strings!
                if k_data.get("synopsis"): 
                    ep.synopsis = k_data["synopsis"]
                if k_data.get("thumbnail"): 
                    ep.thumbnail_url = k_data["thumbnail"]