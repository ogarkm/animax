from fastapi import APIRouter, Query, HTTPException
from typing import List
import asyncio

from app.models.player import SourceOffer, PlayerStateModel
from app.providers.manager import provider_manager

router = APIRouter(tags=["Resolver & Playback Engine"])

@router.get("/sources/{mapped_id}", response_model=List[SourceOffer])
async def get_sources(
    mapped_id: str, 
    episode: int = Query(..., description="Absolute episode number")
):
    """
    ENDPOINT 1: Asks the Provider Manager to ping all registered scrapers concurrently.
    The router automatically detects whether to run Anime or VOD scrapers based on the ID prefix!
    """
    if not mapped_id:
        raise HTTPException(status_code=400, detail="Mapped ID is required")

    # SMART PREFIX ROUTING: Auto-detects media type
    prefix = ''.join([c for c in mapped_id if not c.isdigit()])
    if prefix in ["tt", "tm"]:
        media_type = "movie_tv"
    elif prefix in ["a", "m"]:
        media_type = "anime"
    else:
        media_type = "all"

    # We search for BOTH Sub and Dub concurrently across all scrapers
    sub_task = provider_manager.get_all_source_offers(mapped_id, episode, media_type, is_dub=False)
    dub_task = provider_manager.get_all_source_offers(mapped_id, episode, media_type, is_dub=True)
    
    sub_offers, dub_offers = await asyncio.gather(sub_task, dub_task)
    all_offers = sub_offers + dub_offers
    
    if not all_offers:
        raise HTTPException(status_code=404, detail="No streaming sources found for this episode.")
        
    return all_offers

@router.get("/player/payload", response_model=PlayerStateModel)
async def get_player_payload(provider: str, ep_id: str):
    """
    ENDPOINT 2: The player triggers this URL to actually scrape the .m3u8 stream.
    """
    target_provider = provider_manager.get_provider(provider)
    
    if not target_provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found or disabled.")

    try:
        payload = await target_provider.extract_stream(ep_id)
        return payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stream extraction failed: {str(e)}")