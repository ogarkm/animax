from pydantic import BaseModel, Field
from typing import Any, List, Optional
from enum import Enum

class MediaType(str, Enum):
    MOVIE = "movie"
    TV = "tv"
    ANIME = "anime"
    MANGA = "manga"
    LIVETV = "livetv"

class MediaStatus(str, Enum):
    RELEASING = "RELEASING"
    FINISHED = "FINISHED"
    NOT_YET_AIRED = "NOT_YET_AIRED"
    CANCELLED = "CANCELLED"

# --- Home Page / Search Results / Recommendations ---
class BaseMediaCard(BaseModel):
    id: str = Field(..., description="Custom prefixed ID: 'tt1399' (TMDB TV), 'tm155' (TMDB Movie), 'm58567' (MAL), 'a176496' (AniList)")
    title: str
    poster_url: Optional[str] = None
    banner_url: Optional[str] = None
    type: MediaType
    release_year: Optional[int] = None
    rating: Optional[float] = Field(None, description="Normalized to a 10.0 scale")

# --- Episode Structure ---
class EpisodeShort(BaseModel):
    id: str = Field(..., description="Internal episode ID e.g., 'tt1399_e1' or 'm58567_e12'")
    mapped_id: Optional[str] = None  # NEW: e.g., 'm25777' for AOT S2
    absolute_number: int = Field(..., description="Critical for anime scraping")
    season_number: int
    episode_number: int
    title: str
    thumbnail_url: Optional[str] = None
    is_filler: bool = False
    air_date: Optional[str] = None
    synopsis: Optional[str] = None

class ScheduleEntry(BaseModel):
    """One airing episode on the release schedule.

    The project historically grouped entries by weekday, so we keep the old
    'release_date' / 'airing_at' fields as optional compatibility fields while
    still accepting the newer flat schema. The frontend can then choose whether
    to bucket on the client or server without data loss.
    """
    id: str = Field(..., description="AniList-prefixed media id, e.g. 'a176496'")
    title: Optional[str] = Field(None, description="Optional title for legacy compatibility tests")
    poster_url: Optional[str] = None
    banner_url: Optional[str] = None
    type: MediaType = MediaType.ANIME
    episode: Optional[int] = Field(None, description="Episode number airing at airing_at")
    airing_at: int = Field(default=0, description="Unix epoch seconds, UTC")
    release_date: Optional[str] = Field(None, description="Legacy release date used by older schedules")
    release_year: Optional[int] = None
    rating: Optional[float] = Field(None, description="Normalized to a 10.0 scale")

class Season(BaseModel):
    season_number: int
    title: str
    episode_count: int
    poster_url: Optional[str] = None
    mapped_id: Optional[str] = None  # NEW: Maps TMDB season to MAL show
class CastMember(BaseModel):
    name: str
    character: str
    profile_url: Optional[str] = None

class DetailedMedia(BaseModel):
    id: str
    title: str
    type: MediaType
    poster_url: Optional[str] = None
    banner_url: Optional[str] = None
    release_year: Optional[int] = None
    rating: Optional[float] = None
    description: Optional[str] = None
    genres: List[str] = []
    status: MediaStatus
    studios: List[str] = []
    clear_logo_url: Optional[str] = None
    trailer_url: Optional[str] = None
    episodes: List[Any] = []
    seasons: List[Season] = []
    
    # --- RICH METADATA FIELDS ---
    tagline: Optional[str] = None
    runtime: Optional[int] = None
    age_rating: Optional[str] = None
    director: Optional[str] = None
    cast: List[CastMember] = []
    recommendations: List[BaseMediaCard] = []