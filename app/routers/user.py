from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert
from datetime import datetime, timezone

from app.core.database import get_users_db
from app.core.db_models import User, WatchProgress
from app.services.auth_service import get_current_user
from app.models.user import WatchProgressRequest, WatchProgressResponse

router = APIRouter(prefix="/user", tags=["User State & Progress"])

@router.post("/progress", response_model=WatchProgressResponse)
async def save_watch_progress(
    progress: WatchProgressRequest, 
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_users_db)
):
    """
    Saves or updates watch progress. Uses SQLite UPSERT so it overwrites existing data 
    instead of making duplicate rows if the user watches the same episode again.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # SQLite UPSERT statement
    stmt = insert(WatchProgress).values(
        user_id=current_user.id,
        media_id=progress.media_id,
        episode_number=progress.episode_number,
        timestamp=progress.timestamp,
        duration=progress.duration,
        updated_at=now_iso
    )
    
    # If this specific user, media, and episode already exists, UPDATE the timestamp
    update_stmt = stmt.on_conflict_do_update(
        index_elements=['user_id', 'media_id', 'episode_number'],
        set_=dict(
            timestamp=stmt.excluded.timestamp,
            duration=stmt.excluded.duration,
            updated_at=stmt.excluded.updated_at
        )
    )
    
    db.execute(update_stmt)
    db.commit()
    
    return {
        **progress.model_dump(),
        "updated_at": now_iso
    }