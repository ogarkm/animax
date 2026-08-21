import json
import time
from typing import Optional, Any
from sqlalchemy.orm import Session
from app.core.db_models import MetadataCache

class CacheEngine:
    def __init__(self, db: Session):
        self.db = db

    def get(self, cache_key: str) -> Optional[Any]:
        """Fetches data if it exists and hasn't expired."""
        current_time = int(time.time())
        cached_item = self.db.query(MetadataCache).filter(
            MetadataCache.cache_key == cache_key,
            MetadataCache.expires_at > current_time
        ).first()

        if cached_item:
            return json.loads(cached_item.json_data)
        
        # Clean up expired item if found
        expired_item = self.db.query(MetadataCache).filter(MetadataCache.cache_key == cache_key).first()
        if expired_item:
            self.db.delete(expired_item)
            self.db.commit()
            
        return None

    def set(self, cache_key: str, data: Any, ttl_seconds: int = 43200):
        """Saves data to cache (default TTL: 12 hours)."""
        expires_at = int(time.time()) + ttl_seconds
        
        # Check if exists to update or insert
        existing = self.db.query(MetadataCache).filter(MetadataCache.cache_key == cache_key).first()
        
        if existing:
            existing.json_data = json.dumps(data)
            existing.expires_at = expires_at
        else:
            new_cache = MetadataCache(
                cache_key=cache_key,
                json_data=json.dumps(data),
                expires_at=expires_at
            )
            self.db.add(new_cache)
            
        self.db.commit()