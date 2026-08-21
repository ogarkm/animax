import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager
import os

from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import users_engine, mapping_engine, cache_engine
from app.core.database import UsersBase, MappingBase, CacheBase

# IMPORTANT: Import the database models BEFORE create_all()
import app.core.db_models 
from app.workers.background_jobs import sync_fribb_database
from app.services.player_proxy import init_proxy_service, close_proxy_service

# Import the routers
from app.routers import discovery, auth, resolver, user, player

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Animax] Booting up systems...")
    
    # Auto-create database tables
    UsersBase.metadata.create_all(bind=users_engine)
    MappingBase.metadata.create_all(bind=mapping_engine)
    CacheBase.metadata.create_all(bind=cache_engine)
    print("[Animax] SQLite Databases verified & WAL mode active.")
    
    # Initialize Player & Proxy Engine (connection pooling + db overrides)
    await init_proxy_service()
    
    # TRIGGER THE WORKER IN THE BACKGROUND
    # asyncio.create_task allows the server to start immediately while it downloads
    asyncio.create_task(sync_fribb_database())
    
    yield
    
    print("[Animax] Shutting down systems...")
    await close_proxy_service()

app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION, lifespan=lifespan)

# Enable GZip compression for HLS manifests and streaming payloads
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core API Routers
app.include_router(discovery.router, prefix=settings.API_PREFIX)
app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(resolver.router, prefix=settings.API_PREFIX)
app.include_router(user.router, prefix=settings.API_PREFIX)

# Merged Player & HLS Proxy Router (streaming, decryption, live TV, sports, watch party)
app.include_router(player.router)

# Mount player static assets (logo, etc.)
base_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(base_dir)
static_player_dir = os.path.join(root_dir, "static")
if os.path.exists(static_player_dir):
    app.mount("/static", StaticFiles(directory=static_player_dir), name="static")

# Mount main Animax frontend static files at root (as fallback)
animax_site_dir = os.path.join(root_dir, "animax")
app.mount("/", StaticFiles(directory=animax_site_dir, html=True), name="static_site")