"""FastAPI Router for Animax Player, HLS Proxy, Decryption, CDN Live TV & Sports, and Watch Parties."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_cache_db, get_mapping_db
from app.routers.discovery import get_media_details
from app.services.mapping_engine import MappingEngine
from app.providers.metadata.anilist import get_anilist_details
from app.services import player_proxy as pp

logger = logging.getLogger("animax-player-router")

router = APIRouter(tags=["Player & Streaming Engine"])

# --- Universal CORS Headers for Production Deployments ---
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS, POST",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Type",
}

# Resolve template directories
project_root = Path(__file__).resolve().parent.parent.parent
template_dirs = [
    str(project_root / "player" / "templates"),
    str(project_root / "templates"),
    str(project_root / "app" / "templates"),
]
valid_template_dirs = [d for d in template_dirs if os.path.exists(d)]
templates = Jinja2Templates(directory=valid_template_dirs if valid_template_dirs else template_dirs[0])

LOGO_PATH = project_root / "player" / "logo.txt"
if not LOGO_PATH.exists():
    LOGO_PATH = project_root / "logo.txt"
LOGO_TEXT = LOGO_PATH.read_text(encoding="utf-8", errors="replace") if LOGO_PATH.exists() else ""


# --- CORS Preflight Handler ---
@router.options("/hls/{path:path}")
@router.options("/proxy")
@router.options("/fetch/{path:path}")
async def cors_preflight():
    return Response(status_code=204, headers=CORS_HEADERS)


# --- Tunnel Endpoints ---

@router.websocket("/ws/tunnel")
async def websocket_tunnel(websocket: WebSocket):
    await websocket.accept()

    async with pp.tunnel_send_lock:
        if pp.tunnel_websocket is not None:
            await pp.tunnel_websocket.close()
        pp.tunnel_websocket = websocket
        pp.tunnel_connected_at = time.time()

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            message_type = message.get("type")

            if message_type == "response":
                request_id = message.get("id")
                future = pp.tunnel_pending_requests.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
                continue

            if message_type == "pong":
                continue

            if message_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue
    except WebSocketDisconnect:
        pass
    finally:
        if pp.tunnel_websocket is websocket:
            pp.tunnel_websocket = None
            pp.tunnel_connected_at = None
            for future in pp.tunnel_pending_requests.values():
                if not future.done():
                    future.set_exception(RuntimeError("Tunnel disconnected"))
            pp.tunnel_pending_requests.clear()


@router.post("/tunnel/response")
async def tunnel_response(response: dict) -> dict:
    request_id = response.get("id")
    if not request_id:
        raise HTTPException(status_code=400, detail="Missing request id")

    future = pp.tunnel_response_futures.pop(request_id, None)
    if future is None:
        raise HTTPException(status_code=404, detail="Unknown tunnel request id")

    if not future.done():
        future.set_result(response)
    return {"ok": True}


@router.get("/tunnel/poll")
async def tunnel_poll(client_id: Optional[str] = Query(None)) -> dict:
    pp.poll_tunnel_last_seen = time.time()

    async with pp.tunnel_send_lock:
        if pp.tunnel_request_queue:
            return pp.tunnel_request_queue.popleft()

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        pp.tunnel_poll_waiter = future

    try:
        request = await asyncio.wait_for(future, timeout=pp.TUNNEL_POLL_TIMEOUT)
        return request
    except asyncio.TimeoutError:
        return {"type": "noop"}
    finally:
        async with pp.tunnel_send_lock:
            if pp.tunnel_poll_waiter is future:
                pp.tunnel_poll_waiter = None


@router.get("/status")
async def status() -> dict:
    ws_connected = pp.tunnel_websocket is not None
    poll_connected = pp.poll_tunnel_last_seen is not None and (time.time() - pp.poll_tunnel_last_seen) <= pp.TUNNEL_CLIENT_TIMEOUT
    connected_age = None
    if ws_connected and pp.tunnel_connected_at is not None:
        connected_age = time.time() - pp.tunnel_connected_at
    elif poll_connected and pp.poll_tunnel_last_seen is not None:
        connected_age = time.time() - pp.poll_tunnel_last_seen

    return {
        "ok": True,
        "tunnel_connected": ws_connected or poll_connected,
        "tunnel_mode": "websocket" if ws_connected else "poll" if poll_connected else None,
        "connected_age": connected_age,
        "pending_requests": len(pp.tunnel_pending_requests) + len(pp.tunnel_response_futures),
    }


# --- Watch Party Endpoints ---

@router.get("/join", response_class=HTMLResponse)
async def get_join_page(request: Request):
    return templates.TemplateResponse(request, "join.html", {"request": request})


@router.post("/api/party/new")
@router.get("/api/party/new")
async def api_party_new(request: Request):
    title = ""
    media_type = "Movie"
    season = None
    episode = None
    year = None
    logo = None
    synopsis = None
    full_player_path = ""

    if request.method == "POST":
        try:
            body = await request.json()
            title = body.get("title", "")
            season = str(body.get("season")) if body.get("season") not in (None, "None") else None
            episode = str(body.get("episode")) if body.get("episode") not in (None, "None") else None
            year = str(body.get("year")) if body.get("year") not in (None, "None") else None
            logo = body.get("logo")
            synopsis = body.get("synopsis")
            full_player_path = body.get("player_path") or body.get("search_params") or ""
            if body.get("is_live"):
                media_type = "Live Stream"
            elif season and episode:
                media_type = "TV Series"
        except Exception:
            pass
    else:
        title = request.query_params.get("title", "")
        season = request.query_params.get("season")
        episode = request.query_params.get("episode")

    room = pp.create_party_room(
        title=title,
        media_type=media_type,
        season=season,
        episode=episode,
        year=year,
        logo=logo,
        synopsis=synopsis,
        query_params=full_player_path,
    )
    return {"room": room.code, "code": room.code}


@router.get("/api/party/info/{code}")
async def api_party_info(code: str):
    code_clean = code.strip().upper()
    room = pp.party_rooms.get(code_clean)
    if not room:
        return JSONResponse(status_code=404, content={"valid": False, "error": "Party code not found or expired"})

    conns = pp.party_connections.get(code_clean, {})
    member_count = max(1, len(conns))

    if room.query_params:
        base = room.query_params
        if "party=" not in base:
            joiner = "&" if "?" in base else "?"
            redirect_url = f"{base}{joiner}party={code_clean}"
        else:
            redirect_url = base
    else:
        redirect_url = f"/player/stream?party={code_clean}"

    return {
        "valid": True,
        "code": room.code,
        "title": room.title or "Watch Party Stream",
        "media_type": room.media_type,
        "season": room.season,
        "episode": room.episode,
        "year": room.year,
        "logo": room.logo,
        "members": member_count,
        "redirect_url": redirect_url,
    }


@router.websocket("/ws/party/{room_id}")
async def websocket_party(websocket: WebSocket, room_id: str):
    await websocket.accept()
    room_code = room_id.strip().upper()
    client_id = uuid.uuid4().hex[:8]

    async with pp.party_lock:
        if room_code not in pp.party_rooms:
            pp.party_rooms[room_code] = pp.PartyRoom(code=room_code)
        room = pp.party_rooms[room_code]
        conns = pp.party_connections.setdefault(room_code, {})
        conns[client_id] = websocket
        member_count = len(conns)
        snapshot_playing = room.playing
        snapshot_position = pp._party_live_position(room)

    try:
        await websocket.send_text(json.dumps({
            "type": "welcome",
            "client_id": client_id,
            "playing": snapshot_playing,
            "position": snapshot_position,
            "members": member_count,
        }))
        await pp._party_broadcast(room_code, {"type": "members", "members": member_count}, exclude_client_id=client_id)

        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            msg_type = message.get("type")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if msg_type in ("change_episode", "episode"):
                season = str(message.get("season", "") or "")
                episode = str(message.get("episode", "") or "")
                absolute_number = message.get("absolute_number")
                mapped_id = message.get("mapped_id", "")
                ep_title = message.get("title", "")
                player_path = message.get("player_path", "")

                async with pp.party_lock:
                    if season:
                        room.season = season
                    if episode:
                        room.episode = episode
                    if player_path:
                        room.query_params = player_path
                    room.position = 0.0
                    room.updated_at = time.time()
                    room.playing = True

                await pp._party_broadcast(room_code, {
                    "type": "change_episode",
                    "season": season,
                    "episode": episode,
                    "absolute_number": absolute_number,
                    "mapped_id": mapped_id,
                    "title": ep_title,
                    "player_path": player_path,
                    "from": client_id,
                }, exclude_client_id=client_id)
                continue

            if msg_type not in ("play", "pause", "seek", "sync"):
                continue

            try:
                position = float(message.get("position", 0) or 0)
            except (TypeError, ValueError):
                position = 0.0

            async with pp.party_lock:
                room.position = position
                room.updated_at = time.time()
                if msg_type == "play":
                    room.playing = True
                elif msg_type == "pause":
                    room.playing = False
                playing_now = room.playing

            await pp._party_broadcast(room_code, {
                "type": msg_type,
                "position": position,
                "playing": playing_now,
                "from": client_id,
            }, exclude_client_id=client_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Watch party websocket error for room %s", room_code)
    finally:
        member_count = 0
        room_still_open = False
        async with pp.party_lock:
            conns = pp.party_connections.get(room_code)
            if conns is not None:
                conns.pop(client_id, None)
                member_count = len(conns)
                room_still_open = bool(conns)
                if not conns:
                    pp.party_connections.pop(room_code, None)
                    pp.party_rooms.pop(room_code, None)
        if room_still_open:
            await pp._party_broadcast(room_code, {"type": "members", "members": member_count})


# --- HLS Streaming Proxy Endpoints ---

@router.get("/register")
async def register(
    url: str = Query(...),
    referer: Optional[str] = Query(None),
    referrer: Optional[str] = Query(None),
    origin: Optional[str] = Query(None),
    user_agent: Optional[str] = Query(None),
) -> dict:
    url = pp._normalize_stream_url(url)
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")

    referer = referer or referrer
    if referer or origin or user_agent:
        host = pp._normalize_host(url)
        pp._save_host_header_override(host, referer=referer, origin=origin, user_agent=user_agent)

    token = pp.register_stream(url)
    response = {
        "token": token,
        "playlist": f"/hls/{token}/index.m3u8",
    }

    host = pp._normalize_host(url)
    if pp.host_header_overrides.get(host):
        response["applied_headers"] = pp.host_header_overrides[host]

    return JSONResponse(content=response, headers=CORS_HEADERS)


@router.get("/hls/{token}/index.m3u8")
async def serve_playlist(token: str) -> Response:
    upstream_url = pp._url_for_token(token)
    cache_key = pp._playlist_cache_key(upstream_url)
    cached = pp.playlist_cache.get(cache_key)
    if cached:
        ts, text, media_type, ttl = cached
        if ttl > 0 and pp._now() - ts < ttl:
            return Response(content=text, media_type=media_type, headers=CORS_HEADERS)

    resp = await pp._fetch(upstream_url)
    try:
        content_type = pp._guess_media_type(upstream_url, resp.headers)
        if resp.status_code != 200:
            body = await pp._read_small_response(resp)
            raise HTTPException(status_code=resp.status_code, detail=body.decode("utf-8", "replace")[:500])

        body = await resp.aread()
        text = body.decode("utf-8", "replace")

        if not pp._is_playlist_url(upstream_url, content_type) and not text.lstrip().startswith("#EXTM3U"):
            raise HTTPException(status_code=502, detail="Upstream did not return an M3U8 playlist")

        rewritten = pp._rewrite_m3u8(str(resp.url), token, text)
        ttl = pp._playlist_ttl_from_response(resp.headers, text)
        if ttl > 0:
            pp.playlist_cache[cache_key] = (pp._now(), rewritten, "application/vnd.apple.mpegurl", ttl)
        return Response(content=rewritten, media_type="application/vnd.apple.mpegurl", headers=CORS_HEADERS)
    finally:
        await resp.aclose()


@router.get("/hls/{playlist_token}/seg/{seg_token}.{ext}")
async def serve_segment(playlist_token: str, seg_token: str, ext: str, request: Request) -> Response:
    upstream_url = pp._url_for_token(seg_token)
    range_header = request.headers.get("range")
    cache_key = pp._media_cache_key(upstream_url, range_header)
    cached = pp.segment_cache.get(cache_key)
    if cached is not None and not range_header:
        headers = CORS_HEADERS.copy()
        return Response(content=cached, media_type=pp._guess_media_type(upstream_url, httpx.Headers()), headers=headers)

    resp = await pp._fetch(upstream_url, request=request)
    if resp.status_code not in (200, 206):
        body = await pp._read_small_response(resp)
        await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail=body.decode("utf-8", "replace")[:500])

    media_type = pp._guess_media_type(upstream_url, resp.headers)

    resp_headers = CORS_HEADERS.copy()
    for k in ("accept-ranges", "content-length", "content-range", "cache-control", "x-cache-source"):
        val = resp.headers.get(k)
        if val is not None:
            resp_headers["-".join(part.capitalize() for part in k.split("-"))] = val

    if not range_header and media_type in {"video/mp2t", "video/mp4", "video/iso.segment", "audio/aac"}:
        content = await resp.aread()
        await resp.aclose()
        if len(content) <= 8_000_000:
            pp.segment_cache[cache_key] = content
        return Response(
            content=content,
            media_type=media_type,
            headers=resp_headers,
            status_code=resp.status_code,
        )

    async def streamer() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        streamer(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=resp_headers,
    )


@router.get("/hls/{playlist_token}/key/{asset_token}")
async def serve_key(playlist_token: str, asset_token: str, request: Request) -> Response:
    upstream_url = pp._url_for_token(asset_token)
    resp = await pp._fetch(upstream_url, request=request)
    try:
        if resp.status_code != 200:
            body = await pp._read_small_response(resp)
            raise HTTPException(status_code=resp.status_code, detail=body.decode("utf-8", "replace")[:500])
        content = await resp.aread()
        headers = CORS_HEADERS.copy()
        headers["Content-Type"] = resp.headers.get("content-type", pp.MEDIA_CT["key"])
        return Response(content=content, media_type=headers["Content-Type"], headers=headers)
    finally:
        await resp.aclose()


@router.get("/hls/{playlist_token}/init/{asset_token}.{ext}")
async def serve_init(playlist_token: str, asset_token: str, ext: str, request: Request) -> Response:
    upstream_url = pp._url_for_token(asset_token)
    resp = await pp._fetch(upstream_url, request=request)
    try:
        if resp.status_code != 200:
            body = await pp._read_small_response(resp)
            raise HTTPException(status_code=resp.status_code, detail=body.decode("utf-8", "replace")[:500])
        content = await resp.aread()
        headers = CORS_HEADERS.copy()
        media_type = pp._guess_media_type(upstream_url, resp.headers)
        headers["Content-Type"] = media_type
        return Response(content=content, media_type=media_type, headers=headers)
    finally:
        await resp.aclose()


@router.get("/debug/{token}")
async def debug_token(token: str) -> dict:
    return {
        "token": token,
        "upstream": pp._url_for_token(token),
    }


@router.get("/proxy")
async def proxy_passthrough(url: str, request: Request) -> Response:
    """Fallback endpoint for direct proxying when you do not want tokenized routes."""
    resp = await pp._fetch(url, request=request)
    if resp.status_code != 200:
        body = await pp._read_small_response(resp)
        await resp.aclose()
        if url.endswith(".vtt") or url.endswith(".srt") or "/subs/" in url:
            return Response(content="WEBVTT\n\n", media_type="text/vtt", headers=CORS_HEADERS)
        raise HTTPException(status_code=resp.status_code, detail=body.decode("utf-8", "replace")[:500])

    media_type = pp._guess_media_type(url, resp.headers)
    
    resp_headers = CORS_HEADERS.copy()
    for k in ("accept-ranges", "content-length", "content-range", "cache-control"):
        val = resp.headers.get(k)
        if val is not None:
            resp_headers["-".join(part.capitalize() for part in k.split("-"))] = val

    if pp._is_playlist_url(url, media_type):
        try:
            body = await resp.aread()
            text = body.decode("utf-8", "replace")
            return Response(content=text, media_type="application/vnd.apple.mpegurl", headers=resp_headers)
        finally:
            await resp.aclose()

    async def streamer() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        streamer(),
        media_type=media_type,
        headers=resp_headers,
    )


@router.get("/stream")
async def stream_redirect(url: str = Query(...)) -> RedirectResponse:
    normalized_url = pp._normalize_stream_url(url)
    if not normalized_url:
        raise HTTPException(status_code=400, detail="Missing or invalid url")
    
    token = pp.register_stream(normalized_url)
    return RedirectResponse(url=f"/hls/{token}/index.m3u8", status_code=307)


# --- Speedracelight / Videasy Decryption ---

@router.get("/fetch/{tmdb_id}")
async def fetch_and_decrypt(
    tmdb_id: str,
    media_type: str = Query("movie", alias="mediaType"),
    season: Optional[int] = Query(None, alias="seasonId"),
    episode: Optional[int] = Query(None, alias="episodeId"),
    title: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    imdb_id: Optional[str] = Query(None, alias="imdbId"),
    enc: str = Query("2"),
) -> dict:
    data = await pp.fetch_and_decrypt_stream(
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        title=title,
        year=year,
        imdb_id=imdb_id,
        enc=enc,
    )
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/api/player/episodes/{media_id}")
async def get_player_episodes(
    media_id: str,
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
) -> dict:
    try:
        media_details = await get_media_details(media_id=media_id, db=db, map_db=map_db)
        if isinstance(media_details, BaseModel):
            data = media_details.model_dump()
        elif isinstance(media_details, dict):
            data = media_details
        else:
            data = {}

        return JSONResponse(content={
            "media_id": media_id,
            "title": data.get("title", ""),
            "type": data.get("type", "movie"),
            "description": data.get("description", "") or data.get("synopsis", ""),
            "clear_logo_url": data.get("clear_logo_url") or data.get("logo_url"),
            "banner_url": data.get("banner_url"),
            "poster_url": data.get("poster_url"),
            "release_year": data.get("release_year"),
            "rating": data.get("rating"),
            "genres": data.get("genres", []),
            "seasons": data.get("seasons", []),
            "episodes": data.get("episodes", []),
        }, headers=CORS_HEADERS)
    except Exception as e:
        logger.warning("Unable to fetch player episodes for %s: %s", media_id, e)
        return JSONResponse(content={
            "media_id": media_id,
            "title": "",
            "type": "movie",
            "seasons": [],
            "episodes": [],
        }, headers=CORS_HEADERS)


@router.get("/api/player/skip-times/{media_id}")
async def get_skip_times(
    media_id: str,
    episode: int = Query(1, description="Episode number (absolute for anime)"),
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
) -> dict:
    """
    Fetches AniSkip intro/outro/recap skip times for anime playback.
    """
    try:
        mal_id: Optional[int] = None
        clean_id = media_id.strip()

        # 1. Direct MAL id prefix (e.g. 'm16498', 'mal_16498')
        if clean_id.startswith("m") and not clean_id.startswith("movie"):
            num_part = ''.join(c for c in clean_id if c.isdigit())
            if num_part:
                mal_id = int(num_part)

        # 2. Query mapping database
        if not mal_id:
            try:
                engine = MappingEngine(map_db)
                all_ids = engine.get_all_ids(clean_id)
                if all_ids and all_ids.get("mal_id"):
                    mal_id = all_ids.get("mal_id")
            except Exception as me:
                logger.debug("Mapping engine lookup failed for %s: %s", clean_id, me)

        # 3. If AniList ID ('a16498'), lookup AniList details to retrieve idMal
        if not mal_id and clean_id.startswith("a"):
            try:
                num_part = ''.join(c for c in clean_id if c.isdigit())
                if num_part:
                    anilist_data = await get_anilist_details(anilist_id=int(num_part))
                    if anilist_data and anilist_data.get("idMal"):
                        mal_id = int(anilist_data["idMal"])
            except Exception as ae:
                logger.debug("AniList idMal lookup failed for %s: %s", clean_id, ae)

        if not mal_id:
            return JSONResponse(content={"found": False, "skips": []}, headers=CORS_HEADERS)

        # 4. Fetch from AniSkip API
        aniskip_url = f"https://api.aniskip.com/v2/skip-times/{mal_id}/{episode}?types=op&types=ed&types=mixed-op&types=mixed-ed&types=recap&episodeLength=0"
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(aniskip_url, headers={"User-Agent": "Animax/1.0", "Accept": "application/json"})
            if resp.status_code != 200:
                return JSONResponse(content={"found": False, "skips": []}, headers=CORS_HEADERS)
            data = resp.json()
            if not data.get("found"):
                return JSONResponse(content={"found": False, "skips": []}, headers=CORS_HEADERS)

            results = data.get("results", [])
            skips = []
            seen_types = set()
            for r in results:
                stype = r.get("skipType", "")
                interval = r.get("interval", {})
                start = float(interval.get("startTime", 0) or 0)
                end = float(interval.get("endTime", 0) or 0)
                if end <= start:
                    continue

                if stype in ("op", "mixed-op"):
                    norm_type = "intro"
                elif stype in ("ed", "mixed-ed"):
                    norm_type = "outro"
                elif stype == "recap":
                    norm_type = "recap"
                else:
                    norm_type = "intro"

                if norm_type not in seen_types:
                    seen_types.add(norm_type)
                    skips.append({
                        "type": norm_type,
                        "start": start,
                        "end": end,
                    })

            return JSONResponse(content={
                "found": len(skips) > 0,
                "mal_id": mal_id,
                "episode": episode,
                "skips": skips,
            }, headers=CORS_HEADERS)

    except Exception as e:
        logger.warning("Error fetching skip times for %s ep %s: %s", media_id, episode, e)
        return JSONResponse(content={"found": False, "skips": []}, headers=CORS_HEADERS)


# --- CDN Live TV & Sports ---

@router.get("/cdnlivetv/resolve")
async def cdnlivetv_resolve(url: str = Query(...)) -> RedirectResponse:
    m3u8_url = await pp._extract_cdnlivetv_m3u8(url)
    if not m3u8_url:
        raise HTTPException(
            status_code=502, 
            detail="Failed to resolve media stream. The channel may be offline or geo-restricted."
        )

    host = pp._normalize_host(m3u8_url)
    pp._save_host_header_override(
        host=host,
        referer="https://cdnlivetv.is/",
        origin="https://cdnlivetv.is"
    )
    token = pp.register_stream(m3u8_url)
    return RedirectResponse(url=f"/hls/{token}/index.m3u8", status_code=307)


@router.get("/streams")
async def get_streams() -> dict:
    current_time = pp._now()
    async with pp._streams_cache_lock:
        if pp._streams_cache is not None and (current_time - pp._streams_cache_time < 3600):
            return JSONResponse(content=pp._streams_cache, headers=CORS_HEADERS)

        headers = {
            "User-Agent": pp.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        
        try:
            resp = await pp._request_via_tunnel(
                "GET",
                "https://api.cdnlivetv.tv/api/v1/channels/?user=cdnlivetv&plan=free",
                headers=headers,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Failed to pull CDN Live TV streams: {resp.text[:200]}")
            raw_data = resp.json()
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=502, detail=f"CDN Live TV lookup failed: {str(e)}")
            
        raw_channels = raw_data.get("channels", [])
        cleaned_streams = []
        
        for item in raw_channels:
            player_url = item.get("url")
            if not player_url:
                continue

            stream_url = f"/cdnlivetv/resolve?url={quote_plus(player_url)}"
            cleaned_streams.append({
                "id": item.get("name", "").lower().replace(" ", "-"),
                "name": item.get("name"),
                "logo": item.get("image"),
                "category": "Live TV",
                "country": item.get("code"),
                "stream_url": stream_url,
            })
            
        result_payload = {
            "total": len(cleaned_streams),
            "generated": str(int(current_time)),
            "streams": cleaned_streams
        }
        
        pp._streams_cache = result_payload
        pp._streams_cache_time = current_time
        
        return JSONResponse(content=result_payload, headers=CORS_HEADERS)


@router.get("/sports/matches/all")
async def wf_all(date: Optional[str] = None, hasstream: bool = False, sport: Optional[str] = Query(None)):
    data = await pp._handle_sports_matches("all", date, hasstream=hasstream, sport_query=sport)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/popular")
async def wf_popular(date: Optional[str] = None, hasstream: bool = False, sport: Optional[str] = Query(None)):
    data = await pp._handle_sports_matches("popular", date, hasstream=hasstream, sport_query=sport)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/live")
async def wf_live(hasstream: bool = False, sport: Optional[str] = Query(None)):
    data = await pp._handle_sports_matches("live", None, hasstream=hasstream, sport_query=sport)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/popular/live")
async def wf_popular_live(hasstream: bool = False, sport: Optional[str] = Query(None)):
    data = await pp._handle_sports_matches("popular/live", None, hasstream=hasstream, sport_query=sport)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/{sport}")
async def wf_sport(sport: str, date: Optional[str] = None, hasstream: bool = False):
    data = await pp._handle_sports_matches(sport, date, hasstream=hasstream)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/{sport}/popular")
async def wf_sport_popular(sport: str, date: Optional[str] = None, hasstream: bool = False):
    data = await pp._handle_sports_matches(f"{sport}/popular", date, hasstream=hasstream)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/sports/matches/{sport}/live")
async def wf_sport_live(sport: str, hasstream: bool = False):
    data = await pp._handle_sports_matches(f"{sport}/live", None, hasstream=hasstream)
    return JSONResponse(content=data, headers=CORS_HEADERS)


@router.get("/api/stream/metadata")
async def get_stream_metadata(token: str = Query(...)) -> dict:
    data = await pp.get_stream_metadata_lookup(token)
    return JSONResponse(content=data, headers=CORS_HEADERS)


# --- Player UI Routes ---

@router.get("/player/browse", response_class=HTMLResponse)
async def ui_browse(request: Request):
    return templates.TemplateResponse(request, "browse.html", {"request": request})


@router.get("/player/live", response_class=HTMLResponse)
async def ui_player_live(
    request: Request,
    stream_url: Optional[str] = Query(None, alias="stream_url"),
    stream_sources: Optional[str] = Query(None),
):
    parsed_sources: list[dict] = []
    if stream_sources:
        try:
            parsed_sources = pp._parse_stream_sources_payload(stream_sources)
        except Exception as exc:
            logger.warning("Unable to parse stream sources for /player/live: %s", exc)

    if not parsed_sources and stream_url:
        parsed_sources = [
            {
                "id": "live-primary",
                "title": "Live stream",
                "type": "live",
                "url": stream_url,
                "is_default": True,
            }
        ]

    active_stream_url = stream_url or (parsed_sources[0]["url"] if parsed_sources else "")
    if active_stream_url and parsed_sources and not any(source.get("url") == active_stream_url for source in parsed_sources):
        parsed_sources.insert(0, {
            "id": "live-primary",
            "title": "Live stream",
            "type": "live",
            "url": active_stream_url,
            "is_default": True,
        })

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "request": request,
            "stream_id": active_stream_url,
            "tmdb_id": None,
            "is_live": True,
            "stream_sources": parsed_sources,
        },
    )


@router.get("/player/stream", response_class=HTMLResponse)
async def ui_player_stream_general(
    request: Request,
    stream_url: Optional[str] = Query(None, alias="stream_url"),
    payload_url: Optional[str] = Query(None, alias="payload_url"),
    provider: Optional[str] = Query(None),
    ep_id: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    logo: Optional[str] = Query(None),
    synopsis: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    seasonId: Optional[int] = Query(None, alias="seasonId"),
    episodeId: Optional[int] = Query(None, alias="episodeId"),
    is_live: bool = Query(False),
    stream_sources: Optional[str] = Query(None),
):
    parsed_sources: list[dict] = []
    if stream_sources:
        try:
            parsed_sources = pp._parse_stream_sources_payload(stream_sources)
        except Exception as exc:
            logger.warning("Unable to parse stream sources for /player/stream: %s", exc)

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "request": request,
            "stream_id": stream_url,
            "tmdb_id": None,
            "payload_url": payload_url,
            "provider": provider,
            "ep_id": ep_id,
            "is_live": is_live,
            "season_id": seasonId,
            "episode_id": episodeId,
            "stream_sources": parsed_sources,
            "meta_title": title,
            "meta_logo": logo,
            "meta_synopsis": synopsis,
            "meta_year": year,
            "meta_imdb_id": None,
        },
    )


@router.get("/player/stream/{tmdb_id}", response_class=HTMLResponse)
async def ui_player_stream(
    request: Request,
    tmdb_id: str,
    seasonId: Optional[int] = Query(None, alias="seasonId"),
    episodeId: Optional[int] = Query(None, alias="episodeId"),
    title: Optional[str] = Query(None),
    logo: Optional[str] = Query(None),
    synopsis: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    imdbId: Optional[str] = Query(None, alias="imdbId"),
):
    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "request": request,
            "stream_id": None,
            "tmdb_id": tmdb_id,
            "payload_url": None,
            "provider": None,
            "ep_id": None,
            "is_live": False,
            "season_id": seasonId,
            "episode_id": episodeId,
            "stream_sources": [],
            "meta_title": title,
            "meta_logo": logo,
            "meta_synopsis": synopsis,
            "meta_year": year,
            "meta_imdb_id": imdbId,
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
@router.get("/favicon.png", include_in_schema=False)
async def favicon():
    return RedirectResponse(url="/static/logo.png", status_code=302)