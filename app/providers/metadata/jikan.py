import httpx
from app.models.media import EpisodeShort

JIKAN_BASE = "https://api.jikan.moe/v4"

async def get_jikan_details(mal_id: int) -> dict:
    """Fetches clean synopsis from MyAnimeList."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{JIKAN_BASE}/anime/{mal_id}")
        if resp.status_code == 200:
            return resp.json().get("data", {})
        return {}

async def get_jikan_episodes(mal_id: int, custom_id: str) -> list:
    """Fetches episodes from MAL and formats them to EpisodeShort."""
    # Note: Jikan paginates episodes (100 per page). For simplicity, we fetch page 1.
    # We can add a pagination loop later if a show has > 100 eps.
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{JIKAN_BASE}/anime/{mal_id}/episodes")
        if resp.status_code != 200:
            return []
            
        episodes_data = resp.json().get("data", [])
        formatted_eps = []
        
        for ep in episodes_data:
            ep_num = ep.get("mal_id")
            formatted_eps.append(
                EpisodeShort(
                    id=f"{custom_id}_e{ep_num}",
                    absolute_number=ep_num,
                    season_number=1, # Anime usually defaults to season 1 in our unified array
                    episode_number=ep_num,
                    title=ep.get("title") or f"Episode {ep_num}",
                    is_filler=ep.get("filler", False),
                    air_date=ep.get("aired", "").split("T")[0] if ep.get("aired") else None
                )
            )
        return formatted_eps