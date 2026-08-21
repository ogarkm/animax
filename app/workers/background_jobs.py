import httpx
import asyncio
from sqlalchemy.orm import Session
from sqlalchemy import delete
from app.core.database import MappingSessionLocal, mapping_engine, MappingBase
from app.core.db_models import AnimeMapping

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"


def _to_int(val):
    """Safely cast value, first element of list, or dict entry to integer."""
    if val is None:
        return None
    if isinstance(val, list):
        if not val:
            return None
        val = val[0]
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        digits = ''.join([c for c in val if c.isdigit()])
        return int(digits) if digits else None
    return None


async def sync_fribb_database():
    """
    Downloads the complete Fribb Anime JSON database and syncs it to SQLite.
    Runs on startup and periodic background refreshes.
    """
    print("[Worker] Starting Fribb Database Sync...")
    
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(FRIBB_URL)
            if resp.status_code != 200:
                print(f"[Worker] Failed to download Fribb data. Status: {resp.status_code}")
                return
            data = resp.json()
    except Exception as e:
        print(f"[Worker] Exception downloading Fribb data: {e}")
        return

    print(f"[Worker] Downloaded {len(data)} anime mappings. Syncing to SQLite...")
    
    # Ensure mapping table schema exists with updated columns
    AnimeMapping.__table__.drop(mapping_engine, checkfirst=True)
    AnimeMapping.__table__.create(mapping_engine, checkfirst=True)

    db: Session = MappingSessionLocal()
    try:
        bulk_objects = []
        for item in data:
            mal_id = _to_int(item.get("mal_id"))
            anilist_id = _to_int(item.get("anilist_id"))
            kitsu_id = _to_int(item.get("kitsu_id"))
            tvdb_id = _to_int(item.get("tvdb_id"))
            
            tmdb_obj = item.get("themoviedb_id")
            tmdb_tv_id = None
            tmdb_movie_id = None
            if isinstance(tmdb_obj, dict):
                tmdb_tv_id = _to_int(tmdb_obj.get("tv"))
                tmdb_movie_id = _to_int(tmdb_obj.get("movie"))
            elif isinstance(tmdb_obj, (int, str, list)):
                # Default numeric themoviedb_id based on anime type if present
                item_type = str(item.get("type", "")).upper()
                if item_type == "MOVIE":
                    tmdb_movie_id = _to_int(tmdb_obj)
                else:
                    tmdb_tv_id = _to_int(tmdb_obj)

            season_obj = item.get("season")
            tmdb_season = None
            if isinstance(season_obj, dict):
                tmdb_season = _to_int(season_obj.get("tmdb"))
            elif isinstance(season_obj, (int, str, list)):
                tmdb_season = _to_int(season_obj)

            # Skip entries that have no usable mapping IDs whatsoever
            if not any([mal_id, anilist_id, kitsu_id, tvdb_id, tmdb_tv_id, tmdb_movie_id]):
                continue

            mapping = AnimeMapping(
                mal_id=mal_id,
                anilist_id=anilist_id,
                kitsu_id=kitsu_id,
                tvdb_id=tvdb_id,
                tmdb_tv_id=tmdb_tv_id,
                tmdb_movie_id=tmdb_movie_id,
                tmdb_season=tmdb_season,
            )
            bulk_objects.append(mapping)
            
        # Bulk save for maximum speed
        db.bulk_save_objects(bulk_objects)
        db.commit()
        print(f"[Worker] Fribb Database Sync Complete! Inserted {len(bulk_objects)} records.")
        
    except Exception as e:
        db.rollback()
        print(f"[Worker] Database Sync Failed: {e}")
    finally:
        db.close()