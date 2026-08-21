from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from fastapi.security import OAuth2PasswordRequestForm

from app.core.database import get_users_db
from app.core.db_models import User
from app.core.security import get_password_hash, verify_password, create_access_token
from app.models.user import UserCreate, Token, UserProfile

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=UserProfile)
async def register(user: UserCreate, db: Session = Depends(get_users_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    hashed_password = get_password_hash(user.password)
    now_iso = datetime.now(timezone.utc).isoformat()
    
    new_user = User(
        username=user.username,
        password_hash=hashed_password,
        created_at=now_iso
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_users_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = create_access_token(data={"username": user.username})
    return {"access_token": access_token, "token_type": "bearer"}