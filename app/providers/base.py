import json
import base64
import httpx
from typing import List, Optional, Dict, Any
from app.models.player import SourceOffer, PlayerStateModel, MediaContext

def encode_ep_id(payload: dict) -> str:
    """Safely encodes a dictionary payload into a URL-safe Base64 token."""
    raw_json = json.dumps(payload, separators=(',', ':'))
    return base64.urlsafe_b64encode(raw_json.encode('utf-8')).decode('utf-8')

def decode_ep_id(token: str) -> dict:
    """Decodes a Base64-encoded JSON token with fallback for legacy unencoded string IDs."""
    if not token:
        return {}
    try:
        decoded_bytes = base64.urlsafe_b64decode(token.encode('utf-8'))
        decoded_str = decoded_bytes.decode('utf-8')
        if decoded_str.startswith('{') and decoded_str.endswith('}'):
            return json.loads(decoded_str)
    except Exception:
        pass
    # Legacy fallback for plain string tokens (e.g. '123_sub', 'tv_123_s1_e1')
    return {"raw": token}

class BaseProvider:
    """
    The interface that all scrapers must inherit from.
    """
    name: str = "Base"
    provider_type: str = "anime" # 'anime', 'movie', 'tv', 'movie_tv'

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or getattr(self._client, 'is_closed', False) is True:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    async def get_source_offers(
        self, 
        mapped_id: str, 
        episode_absolute: int, 
        is_dub: bool = False,
        context: Optional[MediaContext] = None
    ) -> List[SourceOffer]:
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