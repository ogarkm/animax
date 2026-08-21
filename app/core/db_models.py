from sqlalchemy import Column, Integer, String, Float, Boolean, Text, ForeignKey, UniqueConstraint
from app.core.database import UsersBase, MappingBase, CacheBase

# ==========================================
# 1. USERS DATABASE (users.db)
# ==========================================
class User(UsersBase):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    created_at = Column(String, nullable=False) # Store ISO datetime strings for SQLite

class WatchProgress(UsersBase):
    __tablename__ = "watch_progress"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_id = Column(String, nullable=False, index=True) # e.g., 'tt123', 'tm155', or 'm58567'
    episode_number = Column(Integer, nullable=False)
    timestamp = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)
    updated_at = Column(String, nullable=False)
    
    # This prevents duplicate rows. Allows us to UPSERT (overwrite) progress!
    __table_args__ = (UniqueConstraint('user_id', 'media_id', 'episode_number', name='uix_user_media_ep'),)

# ==========================================
# 2. MAPPING DATABASE (mapping.db)
# ==========================================
class AnimeMapping(MappingBase):
    __tablename__ = "anime_mappings"
    
    # Auto-incrementing primary key allows saving all mappings even when mal_id is null
    id = Column(Integer, primary_key=True, autoincrement=True)
    mal_id = Column(Integer, index=True, nullable=True) 
    anilist_id = Column(Integer, index=True, nullable=True)
    tmdb_tv_id = Column(Integer, index=True, nullable=True)
    tmdb_movie_id = Column(Integer, index=True, nullable=True)
    kitsu_id = Column(Integer, index=True, nullable=True)
    tvdb_id = Column(Integer, index=True, nullable=True)
    tmdb_season = Column(Integer, index=True, nullable=True)

# ==========================================
# 3. CACHE DATABASE (cache.db)
# ==========================================
class MetadataCache(CacheBase):
    __tablename__ = "metadata_cache"
    
    cache_key = Column(String, primary_key=True, index=True) # e.g., "details_tmdb_123"
    json_data = Column(Text, nullable=False) # Store the heavy JSON dump here
    expires_at = Column(Integer, nullable=False, index=True) # Unix timestamp

class ScraperHealth(CacheBase):
    __tablename__ = "scraper_health"
    
    provider_name = Column(String, primary_key=True, index=True) # e.g., "hianime"
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    last_failed_at = Column(Integer, nullable=True)