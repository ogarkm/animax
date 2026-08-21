from typing import List, Optional
from app.models.player import SourceOffer, PlayerStateModel

class BaseProvider:
    """
    The interface that all scrapers must inherit from.
    """
    name: str = "Base"
    provider_type: str = "anime" # 'anime', 'movie', 'tv'
    
    async def get_source_offers(self, mapped_id: str, episode_absolute: int, is_dub: bool = False) -> List[SourceOffer]:
        """
        ENDPOINT 1: Fast check to see if this provider has the episode.
        Returns a SourceOffer with the internal trigger URL.
        """
        raise NotImplementedError()

    async def extract_stream(self, provider_ep_id: str) -> PlayerStateModel:
        """
        ENDPOINT 2: The heavy lifter. Scrapes the actual .m3u8, subtitles, and skip times.
        """
        raise NotImplementedError()