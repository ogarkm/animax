from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

# --- Auth Schemas ---
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class TokenData(BaseModel):
    username: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6)

# --- Profile Schemas ---
class UserProfile(BaseModel):
    id: int
    username: str
    avatar_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True # Allows Pydantic to read from SQLAlchemy objects

# --- Watch Progress Schemas ---
class WatchProgressRequest(BaseModel):
    media_id: str
    episode_number: int
    timestamp: float
    duration: float

class WatchProgressResponse(BaseModel):
    media_id: str
    episode_number: int
    timestamp: float
    duration: float
    updated_at: datetime

    class Config:
        from_attributes = True