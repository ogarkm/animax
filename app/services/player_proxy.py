"""Core player & HLS proxy service engine.

Handles:
- Upstream HTTP client connection pooling
- Tokenized HLS stream mapping and M3U8 rewriting
- LRU caching for playlists and media segments
- Host header override persistence (proxy.db)
- Direct & websocket/polling tunnel routing
- Speedracelight / Videasy stream decryption
- CDN Live TV stream resolution, sports matching, and metadata lookups
- Watch Party room state synchronization
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlparse, urlunparse, urljoin, quote_plus

import random
import string

import httpx
from cachetools import LRUCache
from fastapi import HTTPException, Request, WebSocket

from app.core.config import settings

logger = logging.getLogger("animax-player-proxy")

# --- Tunables ---
DEFAULT_TIMEOUT = float(os.getenv("PROXY_TIMEOUT", "30"))
MAX_CONNECTIONS = int(os.getenv("PROXY_MAX_CONNECTIONS", "200"))
MAX_KEEPALIVE = int(os.getenv("PROXY_MAX_KEEPALIVE", "50"))
PLAYLIST_TTL = int(os.getenv("PROXY_PLAYLIST_TTL", "300"))
PLAYLIST_CACHE_MAX = int(os.getenv("PROXY_PLAYLIST_CACHE_MAX", "2000"))
SEGMENT_CACHE_MAX = int(os.getenv("PROXY_SEGMENT_CACHE_MAX", "256"))

TOKEN_SECRET = (
    os.getenv("PROXY_TOKEN_SECRET")
    or getattr(settings, "JWT_SECRET_KEY", "")
).encode("utf-8")

USER_AGENT = os.getenv(
    "PROXY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
)
REFERER = os.getenv("PROXY_REFERER", "https://player.videasy.to/")
ORIGIN = os.getenv("PROXY_ORIGIN", "https://player.videasy.to")

UPSTREAM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": REFERER,
    "Origin": ORIGIN,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}

NO_TUNNEL = True
MEDIA_CT = {
    "m3u8": "application/vnd.apple.mpegurl",
    "ts": "video/mp2t",
    "m4s": "video/iso.segment",
    "mp4": "video/mp4",
    "aac": "audio/aac",
    "vtt": "text/vtt",
    "key": "application/octet-stream",
}

# --- Global runtime state ---
client: Optional[httpx.AsyncClient] = None

tunnel_websocket: Optional[WebSocket] = None
tunnel_connected_at: Optional[float] = None
tunnel_pending_requests: Dict[str, asyncio.Future] = {}
tunnel_send_lock = asyncio.Lock()
TUNNEL_REQUEST_TIMEOUT = float(os.getenv("TUNNEL_REQUEST_TIMEOUT", "30"))

poll_tunnel_connected_at: Optional[float] = None
poll_tunnel_last_seen: Optional[float] = None
tunnel_request_queue: deque[Dict[str, Any]] = deque()
tunnel_poll_waiter: Optional[asyncio.Future] = None
tunnel_response_futures: Dict[str, asyncio.Future] = {}
TUNNEL_POLL_TIMEOUT = float(os.getenv("TUNNEL_POLL_TIMEOUT", "25"))
TUNNEL_CLIENT_TIMEOUT = float(os.getenv("TUNNEL_CLIENT_TIMEOUT", "60"))

# token -> upstream URL
url_tokens: Dict[str, str] = {}
reverse_tokens: Dict[str, str] = {}

DB_PATH = Path(getattr(settings, "PROXY_DB_PATH", "proxy.db"))
db_conn: Optional[sqlite3.Connection] = None
host_header_overrides: Dict[str, Dict[str, str]] = {}
db_lock = threading.Lock()

# cache key -> (timestamp, text, media_type, ttl)
playlist_cache: LRUCache = LRUCache(maxsize=PLAYLIST_CACHE_MAX)
# cache key -> bytes
segment_cache: LRUCache = LRUCache(maxsize=SEGMENT_CACHE_MAX)

# Catalogs / In-Memory Caches
_streams_cache: Optional[dict] = None
_streams_cache_time: float = 0.0
_streams_cache_lock = asyncio.Lock()

_watchfooty_cache: Dict[str, tuple[list, float]] = {}
_watchfooty_cache_lock = asyncio.Lock()
_extractor_sem = asyncio.Semaphore(20)


@dataclass(frozen=True)
class UpstreamAsset:
    url: str
    media_type: str
    is_playlist: bool = False


# --- Watch Party Room Dataclass & State ---

@dataclass
class PartyRoom:
    code: str
    playing: bool = False
    position: float = 0.0
    updated_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    title: str = ""
    media_type: str = "Movie"
    season: Optional[str] = None
    episode: Optional[str] = None
    year: Optional[str] = None
    logo: Optional[str] = None
    synopsis: Optional[str] = None
    query_params: str = ""
    mapped_id: Optional[str] = None
    absolute_number: Optional[int] = None
    provider: Optional[str] = None
    ep_id: Optional[str] = None
    stream_url: Optional[str] = None
    dub: Optional[bool] = None


party_rooms: Dict[str, PartyRoom] = {}
party_connections: Dict[str, Dict[str, WebSocket]] = {}
party_lock = asyncio.Lock()


# --- Helpers ---

def _client() -> httpx.AsyncClient:
    global client
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            trust_env=False,
            http2=True,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE,
            )
        )
    return client


def _now() -> float:
    return time.time()


def _resolve_db_path(path: Path) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
        return path
    except Exception as exc:
        fallback = Path(tempfile.gettempdir()) / path.name
        logger.warning(
            "Database path %s is not writable (%s); using fallback %s",
            path,
            exc,
            fallback,
        )
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            with fallback.open("a", encoding="utf-8"):
                pass
            return fallback
        except Exception as exc2:
            logger.error(
                "Fallback database path %s is also not writable: %s",
                fallback,
                exc2,
            )
            return path


def generate_party_code(length: int = 5) -> str:
    """Generate a clean, unambiguous 5-character uppercase alphanumeric code."""
    # Exclude easily confused characters (0, O, 1, I)
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        code = "".join(random.choices(alphabet, k=length))
        if code not in party_rooms:
            return code


def create_party_room(
    title: str = "",
    media_type: str = "Movie",
    season: Optional[str] = None,
    episode: Optional[str] = None,
    year: Optional[str] = None,
    logo: Optional[str] = None,
    synopsis: Optional[str] = None,
    query_params: str = "",
    mapped_id: Optional[str] = None,
    absolute_number: Optional[int] = None,
    provider: Optional[str] = None,
    ep_id: Optional[str] = None,
    stream_url: Optional[str] = None,
    dub: Optional[bool] = None,
) -> PartyRoom:
    code = generate_party_code()
    room = PartyRoom(
        code=code,
        title=title or "Watch Party Stream",
        media_type=media_type,
        season=season,
        episode=episode,
        year=year,
        logo=logo,
        synopsis=synopsis,
        query_params=query_params,
        mapped_id=mapped_id,
        absolute_number=absolute_number,
        provider=provider,
        ep_id=ep_id,
        stream_url=stream_url,
        dub=dub,
    )
    party_rooms[code] = room
    return room

def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def _normalize_host(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith(":80"):
        host = host[:-3]
    elif host.endswith(":443"):
        host = host[:-4]
    return host


def _load_host_header_overrides() -> None:
    global host_header_overrides
    if db_conn is None:
        return
    cursor = db_conn.execute(
        "SELECT host, referer, origin, user_agent FROM host_header_overrides"
    )
    host_header_overrides = {}
    for host, referer, origin, user_agent in cursor.fetchall():
        headers: Dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        if user_agent:
            headers["User-Agent"] = user_agent
        if headers:
            host_header_overrides[host] = headers


def _save_host_header_override(
    host: str,
    referer: Optional[str] = None,
    origin: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    global host_header_overrides
    if db_conn is None:
        return

    existing = host_header_overrides.get(host, {}).copy()
    if referer is not None:
        existing["Referer"] = referer
    if origin is not None:
        existing["Origin"] = origin
    if user_agent is not None:
        existing["User-Agent"] = user_agent
    if not existing:
        return

    host_header_overrides[host] = existing
    sql = """
        INSERT INTO host_header_overrides(host, referer, origin, user_agent, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(host) DO UPDATE SET
            referer = excluded.referer,
            origin = excluded.origin,
            user_agent = excluded.user_agent,
            updated_at = excluded.updated_at
    """
    with db_lock:
        try:
            db_conn.execute(sql, (
                host,
                existing.get("Referer"),
                existing.get("Origin"),
                existing.get("User-Agent"),
                _now(),
            ))
            db_conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Failed to persist host_header_override for %s: %s",
                host,
                exc,
            )


def _token_for_url(url: str) -> str:
    canonical = _canonicalize_url(url)
    token = reverse_tokens.get(canonical)
    if token:
        return token

    payload = base64.urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(TOKEN_SECRET, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    token = f"v1.{payload}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"
    
    if len(url_tokens) >= 10000:
        for _ in range(1000):
            try:
                old_token, old_url = url_tokens.popitem()
                reverse_tokens.pop(old_url, None)
            except KeyError:
                break

    url_tokens[token] = canonical
    reverse_tokens[canonical] = token
    return token


def _url_for_token(token: str) -> str:
    local_url = url_tokens.get(token)
    if local_url:
        return local_url

    try:
        if not token.startswith("v1."):
            raise ValueError
        payload, encoded_signature = token[3:].split(".", 1)
        expected_signature = hmac.new(
            TOKEN_SECRET, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        supplied_signature = base64.urlsafe_b64decode(encoded_signature + "===")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError
        return base64.urlsafe_b64decode(payload + "===").decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=404, detail="Unknown stream token") from exc


def register_stream(url: str) -> str:
    """Register an upstream HLS URL and return a public token."""
    return _token_for_url(url)


def _resolve_upstream_headers(url: Optional[str]) -> Dict[str, str]:
    """Generates proper proxy connection headers, applying domain overrides if registered."""
    headers = UPSTREAM_HEADERS.copy()
    if url:
        host = _normalize_host(url)
        override = host_header_overrides.get(host)
        if override:
            headers.update(override)
            
    return headers


def _guess_media_type(url: str, upstream_headers: httpx.Headers) -> str:
    ctype = upstream_headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype:
        return ctype
    ext = PurePosixPath(urlparse(url).path).suffix.lower().lstrip(".")
    return MEDIA_CT.get(ext, "application/octet-stream")


def _is_playlist_url(url: str, content_type: str) -> bool:
    if "mpegurl" in content_type or "x-mpegurl" in content_type:
        return True
    ext = PurePosixPath(urlparse(url).path).suffix.lower()
    return ext in {".m3u8", ".m3u"}


def _join_public_segment(token: str, upstream_url: str) -> str:
    ext = PurePosixPath(urlparse(upstream_url).path).suffix.lower().lstrip(".") or "bin"
    seg_token = _token_for_url(upstream_url)
    return f"/hls/{token}/seg/{seg_token}.{ext}"


def _join_public_key(token: str, upstream_url: str) -> str:
    key_token = _token_for_url(upstream_url)
    return f"/hls/{token}/key/{key_token}"


def _join_public_init(token: str, upstream_url: str) -> str:
    init_token = _token_for_url(upstream_url)
    ext = PurePosixPath(urlparse(upstream_url).path).suffix.lower().lstrip(".") or "bin"
    return f"/hls/{token}/init/{init_token}.{ext}"


def _normalize_stream_url(url: str) -> str:
    url = url.strip()
    if url.startswith(('"', "'")) and url.endswith(('"', "'")) and url[0] == url[-1]:
        url = url[1:-1].strip()
    if url.endswith('%22'):
        url = url[:-3].strip()
    return url


def _rewrite_m3u8(base_url: str, public_token: str, playlist_text: str) -> str:
    """Rewrite all URI-bearing fields to public proxy routes."""
    out: list[str] = []
    uri_re = re.compile(r'URI="([^"]+)"')

    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line:
            out.append(raw_line)
            continue

        if line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-MAP:"):
            def repl(m: re.Match[str]) -> str:
                uri = m.group(1)
                upstream = urljoin(base_url, uri)
                if line.startswith("#EXT-X-KEY:"):
                    return f'URI="{_join_public_key(public_token, upstream)}"'
                return f'URI="{_join_public_init(public_token, upstream)}"'

            out.append(uri_re.sub(repl, raw_line))
            continue

        if line.startswith("#"):
            out.append(raw_line)
            continue

        upstream = urljoin(base_url, line)
        out.append(_join_public_segment(public_token, upstream))

    return "\n".join(out) + "\n"


def _playlist_cache_key(url: str) -> str:
    headers = _resolve_upstream_headers(url)
    vary = f"{url}|{headers.get('Referer','')}|{headers.get('Origin','')}|{headers.get('User-Agent','')}"
    return hashlib.sha256(vary.encode("utf-8")).hexdigest()


def _extract_max_age(cache_control: str) -> Optional[int]:
    for part in cache_control.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _playlist_ttl_from_response(headers: httpx.Headers, playlist_text: str) -> int:
    cache_control = headers.get("cache-control", "").lower()
    if "no-store" in cache_control or "no-cache" in cache_control:
        return 0

    max_age = _extract_max_age(cache_control)
    if max_age is not None:
        return max_age

    if "#EXT-X-ENDLIST" not in playlist_text:
        return 1

    return PLAYLIST_TTL


def _media_cache_key(url: str, range_header: Optional[str]) -> str:
    return hashlib.sha256(f"{url}|{range_header or ''}".encode("utf-8")).hexdigest()


def _make_headers(url: Optional[str] = None, request: Optional[Request] = None, *, preserve_range: bool = True) -> Dict[str, str]:
    headers = _resolve_upstream_headers(url)
    if request is not None and preserve_range:
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header
    return headers


async def _tunnel_request(method: str, url: str, headers: Optional[Dict[str, str]] = None, content: Optional[bytes] = None) -> httpx.Response:
    global tunnel_pending_requests
    if tunnel_websocket is None:
        raise RuntimeError("No tunnel client connected")

    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    tunnel_pending_requests[request_id] = future

    request_payload = {
        "type": "request",
        "id": request_id,
        "method": method,
        "url": url,
        "headers": headers or {},
        "body": base64.b64encode(content).decode("ascii") if content else "",
    }

    async with tunnel_send_lock:
        await tunnel_websocket.send_text(json.dumps(request_payload))

    try:
        response = await asyncio.wait_for(future, timeout=TUNNEL_REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        tunnel_pending_requests.pop(request_id, None)
        raise RuntimeError("Tunnel request timed out")
    finally:
        tunnel_pending_requests.pop(request_id, None)

    if response.get("error"):
        raise RuntimeError(response["error"])

    body = base64.b64decode(response.get("body", ""))
    return httpx.Response(
        status_code=int(response.get("status_code", 502)),
        headers=response.get("headers", {}),
        content=body,
    )


async def _request_via_tunnel(method: str, url: str, headers: Optional[Dict[str, str]] = None, content: Optional[bytes] = None) -> httpx.Response:
    if NO_TUNNEL:
        return await _request_direct(method, url, headers=headers, content=content)

    if tunnel_websocket is not None:
        try:
            return await _tunnel_request(method, url, headers=headers, content=content)
        except Exception:
            pass

    return await _poll_tunnel_request(method, url, headers=headers, content=content)


async def _request_direct(method: str, url: str, headers: Optional[Dict[str, str]] = None, content: Optional[bytes] = None) -> httpx.Response:
    req = _client().build_request(method, url, headers=headers or {}, content=content)
    return await _client().send(req)


async def _poll_tunnel_request(method: str, url: str, headers: Optional[Dict[str, str]] = None, content: Optional[bytes] = None) -> httpx.Response:
    if poll_tunnel_last_seen is None or (time.time() - poll_tunnel_last_seen) > TUNNEL_CLIENT_TIMEOUT:
        raise RuntimeError("No tunnel client connected")

    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    tunnel_response_futures[request_id] = future

    body_b64 = base64.b64encode(content).decode("ascii") if content else ""
    payload: Dict[str, Any] = {
        "type": "request",
        "id": request_id,
        "method": method,
        "url": url,
        "headers": headers or {},
        "body": body_b64,
    }

    async with tunnel_send_lock:
        if tunnel_poll_waiter is not None and not tunnel_poll_waiter.done():
            tunnel_poll_waiter.set_result(payload)
        else:
            tunnel_request_queue.append(payload)

    try:
        response = await asyncio.wait_for(future, timeout=TUNNEL_REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        tunnel_response_futures.pop(request_id, None)
        raise RuntimeError("Tunnel request timed out")

    if response.get("error"):
        raise RuntimeError(response["error"])

    body = base64.b64decode(response.get("body", ""))
    return httpx.Response(
        status_code=int(response.get("status_code", 502)),
        headers=response.get("headers", {}),
        content=body,
    )


async def _fetch(url: str, request: Optional[Request] = None) -> httpx.Response:
    req_headers = _make_headers(url, request)
    try:
        return await _request_via_tunnel("GET", url, headers=req_headers)
    except Exception:
        req = _client().build_request("GET", url, headers=req_headers)
        return await _client().send(req, stream=True)


async def _read_small_response(resp: httpx.Response, limit_bytes: int = 8_000_000) -> bytes:
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit_bytes:
            break
    return bytes(body)


# --- Watch Party Logic ---

def _party_live_position(room: PartyRoom) -> float:
    """Extrapolate playhead position for a room that's currently playing."""
    if room.playing:
        return max(0.0, room.position + (time.time() - room.updated_at))
    return room.position


async def _party_broadcast(room_id: str, message: dict, exclude_client_id: Optional[str] = None) -> None:
    conns = party_connections.get(room_id, {})
    dead: list[str] = []
    for cid, ws in list(conns.items()):
        if cid == exclude_client_id:
            continue
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(cid)
    if dead:
        for cid in dead:
            conns.pop(cid, None)


# --- Speedracelight / Videasy Decryption Engine ---

def _build_speedracelight_params(
    tmdb_id: str,
    media_type: str,
    title: Optional[str] = None,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    seed: Optional[str] = None,
    enc: str = "2",
    is_dub: bool = False,
) -> dict:
    """Build the Speedracelight request params for the encrypted sources endpoint."""
    params = {}
    if title:
        params["title"] = title
    params["mediaType"] = media_type
    
    if year is not None:
        params["year"] = str(year)
        
    params["episodeId"] = str(episode) if episode is not None else "1"
    params["seasonId"] = str(season) if season is not None else "1"
    params["tmdbId"] = tmdb_id
    if imdb_id:
        params["imdbId"] = imdb_id
        
    params["enc"] = enc
    if seed:
        params["seed"] = seed

    # NOTE: unverified against live traffic. Speedracelight's own param naming
    # is camelCase (mediaType/episodeId/seasonId), so "dub" is the closest
    # guess. If sources still come back in the wrong language after this,
    # capture a real request from the video player and check the actual
    # param name/value it sends for dub vs sub — this may need to change to
    # "audio", "isDub", "lang", etc., or the upstream may not support
    # filtering at request time at all (in which case the response-side
    # language sort in fetch_and_decrypt_stream is what actually matters).
    params["dub"] = "true" if is_dub else "false"

    return params


def _extract_seed_from_payload(payload: Any) -> Optional[str]:
    """Extract the seed token from the Speedracelight /seed response."""
    if not isinstance(payload, dict):
        return None
    seed = payload.get("seed")
    if isinstance(seed, str) and seed.strip():
        return seed.strip()
    return None


async def fetch_and_decrypt_stream(
    tmdb_id: str,
    media_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    enc: str = "2",
    is_dub: bool = False,
) -> dict:
    """Fetches, decrypts, auto-registers, and processes Videasy streams."""
    logger.info(
        "[FETCH] Starting fetch for tmdb_id=%s, media_type=%s, season=%s, episode=%s, title=%s, year=%s, imdb_id=%s",
        tmdb_id,
        media_type,
        season,
        episode,
        title,
        year,
        imdb_id,
    )

    seed = None
    try:
        seed_url = str(httpx.URL("https://api.speedracelight.com/seed", params={"mediaId": tmdb_id}))
        logger.info("[FETCH] Requesting Speedracelight seed: %s", seed_url)
        seed_resp = await _request_via_tunnel(
            "GET",
            seed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://player.videasy.to/",
                "Origin": "https://player.videasy.to",
                "Connection": "keep-alive",
                "X-Requested-With": "XMLHttpRequest",
                "Accept-Encoding": "identity",
            },
        )
        if seed_resp.status_code == 200:
            payload = json.loads(seed_resp.content.decode("utf-8", errors="ignore"))
            seed = _extract_seed_from_payload(payload)
            logger.info("[FETCH] Got Speedracelight seed: %s", seed)
        else:
            logger.error("[FETCH] Failed to retrieve seed, status code: %s", seed_resp.status_code)
    except Exception as exc:
        logger.error("[FETCH] Speedracelight seed handshake failed: %s", exc)

    if not seed:
        raise HTTPException(
            status_code=502, 
            detail="Failed to acquire session seed from Speedracelight. The seed request is now required by the API."
        )

    params = _build_speedracelight_params(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        year=year,
        imdb_id=imdb_id,
        season=season,
        episode=episode,
        seed=seed,
        enc=enc,
        is_dub=is_dub,
    )

    providers = ["mb-flix", "cdn", "downloader2", "1movies", "m4uhd"]
    enc_data = None
    last_error_status = None
    last_error_text = ""

    for provider in providers:
        base_url = f"https://api.speedracelight.com/{provider}/sources-with-title"
        try:
            url_with_params = httpx.URL(base_url, params=params)
            logger.info(f"[FETCH] Requesting Videasy ({provider}): {url_with_params}")
            resp = await _request_via_tunnel(
                "GET",
                str(url_with_params),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://player.videasy.to/",
                    "Origin": "https://player.videasy.to",
                    "Connection": "keep-alive",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept-Encoding": "identity",
                },
            )

            if resp.status_code == 200:
                try:
                    enc_data = resp.content.decode('utf-8').strip()
                except UnicodeDecodeError:
                    enc_data = resp.content.decode('utf-8', errors='ignore').strip()
                logger.info(f"[FETCH] Got encrypted data from {provider}, length: {len(enc_data)}")
                break
            else:
                last_error_status = resp.status_code
                last_error_text = repr(resp.content[:200])
        except Exception as e:
            logger.error(f"[FETCH] Videasy fetch failed for {provider}: {e}")
            last_error_status = 502
            last_error_text = str(e)

    if not enc_data:
        raise HTTPException(
            status_code=last_error_status or 502,
            detail=f"Failed to fetch encrypted payload from all providers. Last error: {last_error_status} {last_error_text}"
        )

    try:
        logger.info(f"[FETCH] Requesting decryption from enc-dec.app")
        dec_resp = await _request_via_tunnel(
            "POST",
            "https://enc-dec.app/api/dec-videasy",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://player.videasy.to/",
                "Origin": "https://player.videasy.to",
                "Connection": "keep-alive",
                "X-Requested-With": "XMLHttpRequest",
                "Accept-Encoding": "identity",
            },
            content=json.dumps({"text": enc_data, "id": tmdb_id, "seed": seed}).encode("utf-8"),
        )
        if dec_resp.status_code != 200:
            error_text = repr(dec_resp.content[:200])
            logger.error(f"[FETCH] Decryption error: {error_text}")
            raise HTTPException(status_code=dec_resp.status_code, detail=f"Decryption upstream error: {dec_resp.status_code} {error_text}")
        
        try:
            dec_json = json.loads(dec_resp.content.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as je:
            raise HTTPException(status_code=502, detail=f"Decryption response parse error: {str(je)}")
    except Exception as e:
        logger.error(f"[FETCH] Decryption failed: {e}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=502, detail=f"Decryption handshake failed: {str(e)}")

    result_data = dec_json.get("result", {})
    sources = result_data.get("sources", [])
    subtitles = result_data.get("subtitles", [])

    # Language/dub tokens that mean "English dub" vs "Japanese/original sub".
    # We don't know for certain which field name (if any) the upstream uses
    # per-source, so this checks every plausible one defensively rather than
    # assuming a single schema.
    DUB_HINTS = {"en", "eng", "english", "dub", "dubbed"}
    SUB_HINTS = {"ja", "jp", "jpn", "japanese", "sub", "subbed", "original"}

    def _source_is_dub(s: dict) -> Optional[bool]:
        raw = s.get("language") or s.get("lang") or s.get("audio") or s.get("type") or s.get("track")
        if not raw:
            return None
        raw = str(raw).strip().lower()
        if raw in DUB_HINTS:
            return True
        if raw in SUB_HINTS:
            return False
        return None

    processed_sources = []
    for s in sources:
        raw_url = s.get("url")
        quality = s.get("quality", "Unknown")
        if raw_url:
            normalized = _normalize_stream_url(raw_url)
            _save_host_header_override(
                _normalize_host(normalized),
                referer="https://player.videasy.to/",
                origin="https://player.videasy.to",
                user_agent=USER_AGENT
            )
            token = register_stream(normalized)
            processed_sources.append({
                "quality": quality,
                "url": f"/hls/{token}/index.m3u8",
                "is_dub": _source_is_dub(s),
            })

    # If any source actually carries a recognizable language marker, put
    # sources matching the requested audio first. If none of them do (the
    # upstream schema doesn't expose language at all, or it's a single-track
    # response), leave the original order untouched rather than guessing.
    if any(s["is_dub"] is not None for s in processed_sources):
        processed_sources.sort(
            key=lambda s: 0 if s["is_dub"] == is_dub else (1 if s["is_dub"] is None else 2)
        )

    return {
        "status": 200,
        "season and episode": {"seasonId": season, "episodeId": episode},
        "sources": processed_sources,
        "subtitles": subtitles
    }


# --- CDN Live TV Handlers ---

async def _extract_cdnlivetv_m3u8(player_url: str) -> Optional[str]:
    """Scrapes CDN Live TV player page to decrypt and find the raw m3u8 source URL."""
    parsed_player = urlparse(player_url)
    base_player_origin = f"{parsed_player.scheme}://{parsed_player.netloc}/"
    
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": base_player_origin,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        async with _extractor_sem:
            resp = await _request_via_tunnel("GET", player_url, headers=headers)
            if resp.status_code != 200:
                return None

            html = resp.text
            vars_map = dict(re.findall(r"(?:var|const|let)\s+([A-Za-z0-9_]+)\s*=\s*['\"]([^'\"]*)['\"]", html))

            decoder_name = None
            helper_pattern = re.compile(r"function\s+([A-Za-z0-9_]+)\s*\(\s*s\s*\)\s*\{([\s\S]+?)\}")
            for match in helper_pattern.finditer(html):
                name = match.group(1)
                body = match.group(2)
                if (
                    ".replace(/-/g,'+')" in body
                    and ".replace(/_/g,'/')" in body
                    and "atob(s)" in body
                    and "decodeURIComponent" in body
                ):
                    decoder_name = name
                    break

            if decoder_name is None:
                return None

            concat_pattern = re.compile(
                rf"(?:var|const|let)\s+[A-Za-z0-9_]+\s*=\s*(?:{re.escape(decoder_name)}\([^\)]+\)\s*\+\s*)*{re.escape(decoder_name)}\([^\)]+\)"
            )
            concat_match = concat_pattern.search(html)
            if not concat_match:
                return None

            expression = concat_match.group(0)
            parts = re.findall(
                rf"{re.escape(decoder_name)}\(\s*'([^']*)'\s*\)|{re.escape(decoder_name)}\(\s*([A-Za-z0-9_]+)\s*\)",
                expression,
            )

            decoded_parts = []
            for literal, var_name in parts:
                fragment = literal or vars_map.get(var_name)
                if fragment is None:
                    return None
                
                fragment = fragment.replace('-', '+').replace('_', '/')
                fragment += '=' * ((4 - len(fragment) % 4) % 4)
                decoded_fragment = base64.b64decode(fragment).decode('utf-8', errors='replace')
                decoded_parts.append(decoded_fragment)

            return ''.join(decoded_parts)
    except Exception as e:
        logger.error("Failed to decode CDN Live TV m3u8 from %s: %s", player_url, e)
        return None


async def _register_cdnlivetv_hls(player_url: str) -> Optional[str]:
    m3u8_url = await _extract_cdnlivetv_m3u8(player_url)
    if not m3u8_url:
        return None

    host = _normalize_host(m3u8_url)
    _save_host_header_override(
        host=host,
        referer="https://cdnlivetv.is/",
        origin="https://cdnlivetv.is"
    )

    token = register_stream(m3u8_url)
    return f"/hls/{token}/index.m3u8"


async def _clean_cdnlivetv_match_schema(category_name: str, raw_match: dict) -> dict:
    channels = raw_match.get("channels", [])
    cleaned_streams = []

    for ch in channels:
        ch_url = ch.get("url")
        if ch_url:
            stream_url = f"/cdnlivetv/resolve?url={quote_plus(ch_url)}"
            cleaned_streams.append({
                "id": ch.get("channel_name", "").lower().replace(" ", "-"),
                "source": ch.get("channel_name", "Unknown"),
                "quality": "HD",
                "language": ch.get("channel_code", "en"),
                "url": stream_url,
                "is_direct": False,
            })
            
    return {
        "id": raw_match.get("gameID") or str(uuid.uuid4()),
        "title": f"{raw_match.get('homeTeam', 'Unknown')} vs {raw_match.get('awayTeam', 'Unknown')}",
        "poster": raw_match.get("homeTeamIMG") or raw_match.get("awayTeamIMG") or "",
        "teams": {
            "home": raw_match.get("homeTeam", "Unknown"),
            "away": raw_match.get("awayTeam", "Unknown")
        },
        "status": raw_match.get("status"),
        "minute": raw_match.get("time"),
        "league": raw_match.get("tournament"),
        "sport": category_name,
        "streams": cleaned_streams
    }


async def _handle_sports_matches(
    sport_path_or_param: Optional[str] = None, 
    date: Optional[str] = None, 
    hasstream: bool = False,
    sport_query: Optional[str] = None
) -> list:
    path_str = (sport_path_or_param or "").lower().strip()
    query_sport = (sport_query or "").lower().strip()
    
    is_live_request = "live" in path_str
    
    sport_name = "all"
    if query_sport:
        sport_name = query_sport
    else:
        cleaned_path = path_str.replace("popular", "").replace("live", "").replace("/", "").strip()
        if cleaned_path:
            sport_name = cleaned_path

    ttl = 120
    if is_live_request:
        ttl = 15
    elif date:
        ttl = 1800
        
    cache_key = f"{sport_name}_{path_str}_{date or 'today'}"
    
    async with _watchfooty_cache_lock:
        if cache_key in _watchfooty_cache:
            data, exp = _watchfooty_cache[cache_key]
            if time.time() < exp:
                return [m for m in data if m.get("streams")] if hasstream else data

    known_sports = {"soccer", "nba", "nhl", "nfl"}
    api_sport_segment = ""
    if sport_name in known_sports:
        api_sport_segment = f"{sport_name}/"

    url = f"https://api.cdnlivetv.tv/api/v1/events/sports/{api_sport_segment}?user=cdnlivetv&plan=free"
        
    try:
        resp = await _request_via_tunnel("GET", url, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="CDN Live TV API unavailable")
        data_json = resp.json()
    except Exception as e:
        if cache_key in _watchfooty_cache:
            return _watchfooty_cache[cache_key][0]
        raise HTTPException(status_code=502, detail=f"Match upstream fetch failed: {str(e)}")

    payload_root = data_json.get("cdn-live-tv") or data_json.get("cdnlivetv.is") or data_json
    cdn_data = payload_root if isinstance(payload_root, dict) else {}
    cleaned_matches = []

    for key, value in cdn_data.items():
        if isinstance(value, list):
            category_lower = key.lower()
            if sport_name != "all" and category_lower != sport_name:
                continue
            
            for raw_match in value:
                cleaned = await _clean_cdnlivetv_match_schema(key, raw_match)
                cleaned_matches.append(cleaned)

    async with _watchfooty_cache_lock:
        _watchfooty_cache[cache_key] = (cleaned_matches, time.time() + ttl)

    return [m for m in cleaned_matches if m.get("streams")] if hasstream else cleaned_matches


async def get_stream_metadata_lookup(token: str) -> dict:
    try:
        upstream_url = _url_for_token(token)
    except HTTPException:
        return {
            "title": "Live Stream",
            "subtitle": "Stable Broadcast",
            "logo": "",
            "synopsis": "Live media broadcast stream."
        }

    # Search Sports Cache
    async with _watchfooty_cache_lock:
        for cache_key, (matches, exp) in _watchfooty_cache.items():
            for match in matches:
                for ch in match.get("streams", []):
                    ch_url = ch.get("url", "")
                    if token in ch_url or upstream_url in ch_url:
                        return {
                            "title": match.get("title", "Live Sport"),
                            "subtitle": f"{match.get('league', 'Sports Broadcast')} • {match.get('status', 'LIVE')}",
                            "logo": match.get("poster") or "",
                            "synopsis": f"Live sports broadcast: {match.get('teams', {}).get('home', 'Home')} vs {match.get('teams', {}).get('away', 'Away')}."
                        }

    # Search Live TV Channels Cache
    global _streams_cache
    async with _streams_cache_lock:
        if _streams_cache is not None:
            for stream in _streams_cache.get("streams", []):
                s_url = stream.get("stream_url", "")
                if token in s_url or upstream_url in s_url:
                    return {
                        "title": stream.get("name", "Live Channel"),
                        "subtitle": f"{stream.get('category', 'Live TV')} • {stream.get('country', 'International')}",
                        "logo": stream.get("logo") or "",
                        "synopsis": f"Live 24/7 television channel: {stream.get('name', 'Unknown')}."
                    }

    return {
        "title": "Live Stream",
        "subtitle": "Active Broadcast",
        "logo": "",
        "synopsis": "Stable media broadcast stream."
    }


def _parse_stream_sources_payload(raw_sources: Any) -> list[dict]:
    """Parse stream-source payloads from query strings, JSON lists, or direct strings."""
    parsed_sources: list[dict] = []
    if raw_sources is None:
        return parsed_sources

    if isinstance(raw_sources, str):
        try:
            raw_sources = json.loads(raw_sources)
        except Exception:
            raw_sources = [raw_sources]

    if isinstance(raw_sources, list):
        for item in raw_sources:
            if isinstance(item, dict):
                source_url = item.get("url") or item.get("stream_url") or item.get("href")
                if source_url:
                    parsed_sources.append(
                        {
                            "id": item.get("id") or str(uuid.uuid4()),
                            "title": item.get("title") or item.get("source") or item.get("name") or "Stream",
                            "type": item.get("type") or ("direct" if item.get("is_direct") else "live"),
                            "url": source_url,
                            "is_default": bool(item.get("is_default")),
                        }
                    )
            elif isinstance(item, str) and item:
                parsed_sources.append(
                    {
                        "id": str(uuid.uuid4()),
                        "title": "Stream",
                        "type": "live",
                        "url": item,
                        "is_default": False,
                    }
                )

    return parsed_sources


# --- Lifespan Management ---

async def init_proxy_service() -> None:
    global client, db_conn, DB_PATH
    logger.info("[PlayerProxy] Initializing connection pool and sqlite persistence...")
    client = httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,
        http2=True,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT),
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE,
        )
    )
    DB_PATH = _resolve_db_path(DB_PATH)
    try:
        db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS host_header_overrides (
                host TEXT PRIMARY KEY,
                referer TEXT,
                origin TEXT,
                user_agent TEXT,
                updated_at REAL
            )
            """
        )
        db_conn.commit()
        _load_host_header_overrides()
        logger.info("[PlayerProxy] Host header overrides database ready.")
    except sqlite3.OperationalError as exc:
        logger.warning("[PlayerProxy] Unable to open SQLite DB %s: %s", DB_PATH, exc)
        db_conn = None


async def close_proxy_service() -> None:
    global client, db_conn
    logger.info("[PlayerProxy] Closing resources...")
    if client is not None:
        await client.aclose()
        client = None
    if db_conn is not None:
        db_conn.close()
        db_conn = None