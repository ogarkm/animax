from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from enum import Enum

class SourceType(str, Enum):
    IFRAME = "iframe"     # External embed
    INTERNAL = "internal" # Requires /api/player/payload extraction

class MediaContext(BaseModel):
    mapped_id: str
    primary_title: str
    romaji_title: Optional[str] = None
    english_title: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    mal_id: Optional[int] = None
    anilist_id: Optional[int] = None
    tmdb_tv_id: Optional[int] = None
    tmdb_movie_id: Optional[int] = None

# --- Endpoint 1 Response: /api/sources/{id} ---
class SourceOffer(BaseModel):
    provider: str = Field(..., description="e.g., 'hianime', 'flixhq'")
    type: SourceType
    quality: str = Field(..., description="'1080p', '720p', 'auto'")
    dub: bool
    url: str = Field(..., description="Direct iframe link OR internal trigger URL")
    
    # NEW OPTIONAL FIELD FOR MICROSERVICE STREAMING
    external_player_url: Optional[str] = Field(None, description="URL pointing straight to the microservice HTML player")
# --- Endpoint 2 Response: /api/player/payload ---
class SkipTime(BaseModel):
    type: str = Field(..., description="'intro', 'outro', 'recap'")
    start: float
    end: float

class Track(BaseModel):
    file: str = Field(..., description="URL to the .vtt or .srt file")
    label: str = Field(..., description="'English', 'Spanish', 'Thumbnails'")
    kind: str = Field(..., description="'captions', 'thumbnails'")
    default: bool = False

class PlayerStateModel(BaseModel):
    stream_url: str
    stream_type: str = Field("hls", description="'hls' for .m3u8, 'mp4' for direct")
    headers: Dict[str, str] = Field(default_factory=dict, description="Needed for CORS/Referer bypassing")
    
    tracks: List[Track] = []
    skips: List[SkipTime] = []
    
    # Context injected by the Controller to make the UI look nice
    media_title: str
    episode_title: str
    provider_used: str
    next_episode_id: Optional[str] = None