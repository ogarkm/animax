"""Backwards compatibility shim for player/main.py.

Delegates core functionality to app.services.player_proxy and app.routers.player.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
import os

from app.services.player_proxy import (
    UpstreamAsset,
    PartyRoom,
    USER_AGENT,
    REFERER,
    ORIGIN,
    UPSTREAM_HEADERS,
    NO_TUNNEL,
    MEDIA_CT,
    DEFAULT_TIMEOUT,
    MAX_CONNECTIONS,
    MAX_KEEPALIVE,
    PLAYLIST_TTL,
    PLAYLIST_CACHE_MAX,
    SEGMENT_CACHE_MAX,
    TUNNEL_REQUEST_TIMEOUT,
    TUNNEL_POLL_TIMEOUT,
    TUNNEL_CLIENT_TIMEOUT,
    DB_PATH,
    url_tokens,
    reverse_tokens,
    host_header_overrides,
    playlist_cache,
    segment_cache,
    party_rooms,
    party_connections,
    party_lock,
    _client,
    _now,
    _resolve_db_path,
    _canonicalize_url,
    _normalize_host,
    _load_host_header_overrides,
    _save_host_header_override,
    _token_for_url,
    _url_for_token,
    register_stream,
    _resolve_upstream_headers,
    _guess_media_type,
    _is_playlist_url,
    _join_public_segment,
    _join_public_key,
    _join_public_init,
    _normalize_stream_url,
    _rewrite_m3u8,
    _playlist_cache_key,
    _extract_max_age,
    _playlist_ttl_from_response,
    _media_cache_key,
    _make_headers,
    _tunnel_request,
    _request_via_tunnel,
    _request_direct,
    _poll_tunnel_request,
    _fetch,
    _read_small_response,
    _party_live_position,
    _party_broadcast,
    _build_speedracelight_params,
    _extract_seed_from_payload,
    fetch_and_decrypt_stream,
    _extract_cdnlivetv_m3u8,
    _register_cdnlivetv_hls,
    _clean_cdnlivetv_match_schema,
    _handle_sports_matches,
    get_stream_metadata_lookup,
    _parse_stream_sources_payload,
    init_proxy_service,
    close_proxy_service,
)
from app.routers.player import router, templates

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_proxy_service()
    yield
    await close_proxy_service()

app = FastAPI(title="HLS Proxy", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")