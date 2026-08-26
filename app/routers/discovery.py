from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Optional, Any
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import asyncio
import httpx
import re
import os

from app.core.database import get_cache_db, get_mapping_db
from app.core.config import settings
from app.services.cache_engine import CacheEngine
from app.services.mapping_engine import MappingEngine
from app.models.media import BaseMediaCard, DetailedMedia, MediaType, MediaStatus, Season, CastMember, ScheduleEntry

from app.providers.metadata.tmdb import fetch_tmdb_trending, search_tmdb, get_tmdb_details, IMG_BASE
from app.providers.metadata.anilist import (
    fetch_anilist_trending, search_anilist, get_anilist_details, get_anilist_season_chain,
    fetch_anilist_schedule,
)
from app.providers.metadata.jikan import get_jikan_details
from app.providers.metadata.kitsu import get_kitsu_episodes, enrich_episodes_with_kitsu
from app.providers.metadata.tmdb import get_tmdb_seasons_and_episodes

router = APIRouter(tags=["Discovery (Home, Search & Details)"])

TMDB_API_KEY = getattr(settings, "TMDB_API_KEY", os.getenv("TMDB_API_KEY", "your_fallback_api_key_here"))
import zoneinfo
from datetime import datetime, timezone, timedelta

def group_schedule_by_weekday(entries: List[dict]) -> Dict[str, List[dict]]:
    """Legacy compatibility helper: buckets a schedule into the stable
    monday..sunday order expected by the older UI/tests.

    Prefer the explicit release_date when available because legacy fixtures and
    older server payloads encoded the weekday there; only fall back to airing_at
    when no release date is present.
    """
    ordered_days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    grouped = {day: [] for day in ordered_days}

    def resolve_weekday(item: dict) -> Optional[str]:
        release_date = (item or {}).get("release_date")
        if release_date:
            try:
                return datetime.strptime(release_date, "%Y-%m-%d").strftime("%A").lower()
            except ValueError:
                pass

        airing_at = int((item or {}).get("airing_at") or 0)
        if airing_at:
            return datetime.fromtimestamp(airing_at, tz=timezone.utc).strftime("%A").lower()
        return None

    for item in sorted(entries, key=lambda entry: (entry or {}).get("release_date") or (entry or {}).get("airing_at") or ""):
        weekday_name = resolve_weekday(item)
        if weekday_name in grouped:
            grouped[weekday_name].append(item)

    return grouped


def normalize_list(lst: Optional[List[Any]]) -> List[BaseMediaCard]:
    """
    Safely converts a raw list of dictionaries or Pydantic models into a 
    standardized List[BaseMediaCard] to prevent any dictionary 'AttributeError' conflicts.
    """
    if not lst:
        return []
        
    normalized = []
    for item in lst:
        if isinstance(item, BaseMediaCard):
            normalized.append(item)
        elif isinstance(item, dict):
            try:
                # Handle possible variant keys gracefully
                m_id = item.get("id")
                title = item.get("title") or item.get("name")
                poster = item.get("poster_url") or item.get("poster_path")
                banner = item.get("banner_url") or item.get("backdrop_path") or item.get("bannerImage")
                
                # Normalize media types safely
                m_type = item.get("type")
                if isinstance(m_type, str):
                    m_type = MediaType(m_type.lower())
                elif not m_type:
                    m_type = MediaType.MOVIE
                    
                year = item.get("release_year") or item.get("seasonYear")
                rating = item.get("rating")
                if rating is not None:
                    rating = float(rating)

                normalized.append(BaseMediaCard(
                    id=str(m_id),
                    title=str(title),
                    poster_url=poster if str(poster).startswith("http") else f"{IMG_BASE}{poster}" if poster else None,
                    banner_url=banner if str(banner).startswith("http") else f"https://image.tmdb.org/t/p/w1280{banner}" if banner else None,
                    type=m_type,
                    release_year=int(year) if year else None,
                    rating=rating
                ))
            except Exception as e:
                # Silently skip items that don't match structural fields
                print(f"[Normalization Skip]: Failed to parse item dictionary: {e}")
                pass
    return normalized


def strip_known_anime(cards: List[BaseMediaCard], mapper: MappingEngine) -> List[BaseMediaCard]:
    """
    TMDB indexes most anime as plain tv/movie entries. AniList already
    supplies these correctly typed as ANIME, so any TMDB card the mapping DB
    recognizes as anime gets dropped here — prevents duplicate cards
    (e.g. a 'tt' AND an 'a' card for Jujutsu Kaisen) and prevents anime from
    showing up mistyped in the movies/tv buckets.
    """
    if not cards:
        return cards

    tv_ids = [int(c.id[2:]) for c in cards if c.id.startswith("tt") and c.id[2:].isdigit()]
    movie_ids = [int(c.id[2:]) for c in cards if c.id.startswith("tm") and c.id[2:].isdigit()]

    anime_tv_ids = mapper.get_anime_tmdb_tv_ids(tv_ids)
    anime_movie_ids = mapper.get_anime_tmdb_movie_ids(movie_ids)

    def is_known_anime(card: BaseMediaCard) -> bool:
        if card.id.startswith("tt") and card.id[2:].isdigit():
            return int(card.id[2:]) in anime_tv_ids
        if card.id.startswith("tm") and card.id[2:].isdigit():
            return int(card.id[2:]) in anime_movie_ids
        return False

    return [c for c in cards if not is_known_anime(c)]


def pick_spotlight(
    movies: List[dict | BaseMediaCard],
    tv: List[dict | BaseMediaCard],
    anime: List[dict | BaseMediaCard],
    items_per_category: int = 3
) -> List[BaseMediaCard]:
    """
    Selects culturally relevant, high-buzz spotlight items.
    Filters out niche 1-vote 10/10 TMDB anomalies and guarantees balanced representation.
    """
    def filter_and_rank(pool: list, is_tmdb: bool = True) -> List[BaseMediaCard]:
        candidates = []
        for item in pool:
            data = item if isinstance(item, dict) else item.model_dump()
            
            # Must have a valid high-res banner
            if not data.get("banner_url"):
                continue

            rating = data.get("rating") or 0
            vote_count = data.get("vote_count", 100) # AniList doesn't supply raw vote count, defaults safe

            # Exclude TMDB ghost entries with low vote counts and low ratings
            if is_tmdb and vote_count < 50:
                continue
            if rating < 6.5: # Skip poorly reviewed media
                continue

            candidates.append(BaseMediaCard(
                id=str(data["id"]),
                title=str(data["title"]),
                poster_url=data.get("poster_url"),
                banner_url=data.get("banner_url"),
                type=MediaType(data["type"]),
                release_year=data.get("release_year"),
                rating=rating
            ))

        # Deduplicate preserving original list order (which already reflects trending rank)
        seen_ids = set()
        deduped = []
        for c in candidates:
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                deduped.append(c)
        return deduped

    top_movies = filter_and_rank(movies, is_tmdb=True)[:items_per_category]
    top_tv = filter_and_rank(tv, is_tmdb=True)[:items_per_category]
    top_anime = filter_and_rank(anime, is_tmdb=False)[:items_per_category]

    # Interleave to create a balanced carousel (e.g. Movie -> Anime -> TV -> Movie -> Anime -> TV)
    spotlight: List[BaseMediaCard] = []
    max_len = max(len(top_movies), len(top_tv), len(top_anime))
    for i in range(max_len):
        if i < len(top_anime):
            spotlight.append(top_anime[i])
        if i < len(top_movies):
            spotlight.append(top_movies[i])
        if i < len(top_tv):
            spotlight.append(top_tv[i])

    return spotlight

# ==========================================
# ADVANCED METADATA FETCHING CORE ENGINE
# ==========================================

async def fetch_tmdb_list_safe(media_type: str, list_type: str, page: int = 1) -> List[BaseMediaCard]:
    try:
        url = f"https://api.themoviedb.org/3/{media_type}/{list_type}"
        params = {"api_key": TMDB_API_KEY, "page": page, "language": "en-US"}
        
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(url, params=params)
            if response.status_code != 200:
                return []
                
            results = response.json().get("results", [])
            cards = []
            for item in results:
                release_date = item.get("release_date") or item.get("first_air_date") or ""
                year = int(release_date.split("-")[0]) if release_date else None
                
                cards.append(BaseMediaCard(
                    id=f"{'tt' if media_type == 'tv' else 'tm'}{item['id']}",
                    title=item.get("title") or item.get("name"),
                    poster_url=f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get('poster_path') else None,
                    banner_url=f"https://image.tmdb.org/t/p/w1280{item['backdrop_path']}" if item.get('backdrop_path') else None,
                    type=MediaType.TV if media_type == "tv" else MediaType.MOVIE,
                    release_year=year,
                    rating=round(item.get("vote_average", 0), 1)
                ))
            return cards
    except Exception as e:
        print(f"[TMDB list fetch error]: {e}")
        return []

async def fetch_anilist_list_safe(format_type: str, sort_by: List[str], status: Optional[str] = None, page: int = 1) -> List[BaseMediaCard]:
    query = """
    query ($page: Int, $perPage: Int, $sort: [MediaSort], $status: MediaStatus, $type: MediaType) {
      Page (page: $page, perPage: $perPage) {
        media (sort: $sort, status: $status, type: $type) {
          id
          title {
            english
            romaji
          }
          coverImage {
            extraLarge
          }
          bannerImage
          seasonYear
          startDate {
            year
          }
          averageScore
        }
      }
    }
    """
    variables = {
        "page": page,
        "perPage": 20,
        "sort": sort_by,
        "type": format_type
    }
    if status:
        variables["status"] = status

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.post("https://graphql.anilist.co", json={"query": query, "variables": variables})
            if response.status_code != 200:
                return []
                
            media_items = response.json().get("data", {}).get("Page", {}).get("media", [])
            cards = []
            for m in media_items:
                score = m.get("averageScore")
                rating = round(score / 10, 1) if score else None
                year = m.get("seasonYear") or (m.get("startDate", {}) or {}).get("year")
                
                cards.append(BaseMediaCard(
                    id=f"{'mg' if format_type == 'MANGA' else 'a'}{m['id']}",
                    title=m["title"]["english"] or m["title"]["romaji"],
                    poster_url=m["coverImage"]["extraLarge"],
                    banner_url=m["bannerImage"],
                    type=MediaType.MANGA if format_type == "MANGA" else MediaType.ANIME,
                    release_year=year,
                    rating=rating
                ))
            return cards
    except Exception as e:
        print(f"[AniList list fetch error]: {e}")
        return []


# ==========================================
# UPGRADED ROUTE ROUTERS
# ==========================================

@router.get("/home", response_model=Dict[str, List[BaseMediaCard]])
async def get_home_page_api_home_get(
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
):
    cache = CacheEngine(db)
    cache_key = "home_page_trending_advanced_v6"
    
    if cached_data := cache.get(cache_key):
        return cached_data

    # Fetch raw feeds concurrently
    results = await asyncio.gather(
        fetch_tmdb_trending("movie"),                          # 0
        fetch_tmdb_trending("tv"),                             # 1
        fetch_anilist_trending(),                              # 2
        fetch_anilist_list_safe("MANGA", ["TRENDING_DESC"]),   # 3
        
        fetch_tmdb_list_safe("movie", "popular"),              # 4
        fetch_tmdb_list_safe("tv", "popular"),                 # 5
        fetch_anilist_list_safe("ANIME", ["POPULAR_DESC"]),    # 6
        fetch_anilist_list_safe("MANGA", ["POPULAR_DESC"]),    # 7
        
        fetch_tmdb_list_safe("movie", "top_rated"),            # 8
        fetch_tmdb_list_safe("tv", "top_rated"),               # 9
        fetch_anilist_list_safe("ANIME", ["SCORE_DESC"]),      # 10
        fetch_anilist_list_safe("MANGA", ["SCORE_DESC"]),      # 11
        
        fetch_tmdb_list_safe("movie", "upcoming"),             # 12
    )

    mapper = MappingEngine(map_db)

    # 1. Normalize all lists and strip duplicate TMDB anime cards
    movies_trend = strip_known_anime(normalize_list(results[0]), mapper)
    tv_trend = strip_known_anime(normalize_list(results[1]), mapper)
    anime_trend = normalize_list(results[2])
    manga_trend = normalize_list(results[3])
    
    movies_pop = strip_known_anime(normalize_list(results[4]), mapper)
    tv_pop = strip_known_anime(normalize_list(results[5]), mapper)
    anime_pop = normalize_list(results[6])
    manga_pop = normalize_list(results[7])
    
    movies_top = strip_known_anime(normalize_list(results[8]), mapper)
    tv_top = strip_known_anime(normalize_list(results[9]), mapper)
    anime_top = normalize_list(results[10])
    manga_top = normalize_list(results[11])
    
    movies_up = strip_known_anime(normalize_list(results[12]), mapper)

    # 2. Build candidate pools combining Trending + Popular for maximum cultural relevance
    movie_candidates = results[0] + results[4]
    tv_candidates = results[1] + results[5]
    anime_candidates = results[2] + results[6]

    # 3. Generate balanced 9-item spotlight (3 Anime, 3 Movies, 3 TV series)
    spotlight_items = pick_spotlight(
        movies=movie_candidates,
        tv=tv_candidates,
        anime=anime_candidates,
        items_per_category=3,
    )

    home_data = {
        "spotlight": spotlight_items,
        
        "trending_movies": movies_trend,
        "trending_tv": tv_trend,
        "trending_anime": anime_trend,
        "trending_manga": manga_trend,
        
        "popular_movies": movies_pop,
        "popular_tv": tv_pop,
        "popular_anime": anime_pop,
        "popular_manga": manga_pop,
        
        "top_rated_movies": movies_top,
        "top_rated_tv": tv_top,
        "top_rated_anime": anime_top,
        "top_rated_manga": manga_top,
        
        "upcoming_movies": movies_up
    }

    # Cache response for 6 hours
    cache.set(
        cache_key,
        {k: [item.model_dump() for item in v] for k, v in home_data.items()},
        ttl_seconds=21600
    )
    return home_data


@router.get("/discovery", response_model=List[BaseMediaCard])
async def search_discovery_catalog(
    type: MediaType, 
    sort: str = "popular", 
    page: int = 1,
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
):
    cache = CacheEngine(db)
    cache_key = f"discovery_{type.value}_{sort}_{page}"
    
    if cached_data := cache.get(cache_key):
        return cached_data

    raw_results = [] # BUG FIX: Explicity initialize results list here

    if type == MediaType.MOVIE or type == MediaType.TV:
        tmdb_type = "movie" if type == MediaType.MOVIE else "tv"
        if sort == "trending":
            raw_results = await fetch_tmdb_trending(tmdb_type)
        elif sort == "top_rated":
            raw_results = await fetch_tmdb_list_safe(tmdb_type, "top_rated", page)
        elif sort == "upcoming":
            list_endpoint = "upcoming" if type == MediaType.MOVIE else "on_the_air"
            raw_results = await fetch_tmdb_list_safe(tmdb_type, list_endpoint, page)
        else:
            raw_results = await fetch_tmdb_list_safe(tmdb_type, "popular", page)

    elif type == MediaType.ANIME or type == MediaType.MANGA:
        anilist_type = "ANIME" if type == MediaType.ANIME else "MANGA"
        if sort == "trending":
            raw_results = await fetch_anilist_list_safe(anilist_type, ["TRENDING_DESC"], page=page)
        elif sort == "top_rated":
            raw_results = await fetch_anilist_list_safe(anilist_type, ["SCORE_DESC"], page=page)
        elif sort == "upcoming":
            raw_results = await fetch_anilist_list_safe(anilist_type, ["POPULAR_DESC"], status="NOT_YET_RELEASED", page=page)
        else:
            raw_results = await fetch_anilist_list_safe(anilist_type, ["POPULAR_DESC"], page=page)

    normalized = normalize_list(raw_results)

    # Movies/TV browsing can still surface TMDB-sourced anime titles — strip
    # them the same way /home does, since AniList's own ANIME/MANGA tab is
    # the correct place for those.
    if type in (MediaType.MOVIE, MediaType.TV):
        normalized = strip_known_anime(normalized, MappingEngine(map_db))

    cache.set(cache_key, [item.model_dump() for item in normalized], ttl_seconds=14400)
    return normalized


@router.get("/search", response_model=List[BaseMediaCard])
async def search_all_api_search_get(
    query: str,
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
):
    cache = CacheEngine(db)
    cache_key = f"search_{query.lower().replace(' ', '_')}"
    
    if cached_data := cache.get(cache_key):
        return cached_data

    tmdb_results, anilist_results = await asyncio.gather(
        search_tmdb(query),
        search_anilist(query)
    )

    # Drop any TMDB card that the mapping DB recognizes as anime — the
    # AniList card for the same title already exists in anilist_results and
    # is correctly typed, so this is what makes a search for e.g. "Jujutsu
    # Kaisen" resolve to the anime card instead of showing both.
    mapper = MappingEngine(map_db)
    tmdb_cards = strip_known_anime(normalize_list(tmdb_results), mapper)

    combined = tmdb_cards + anilist_results
    normalized = normalize_list(combined)
    cache.set(cache_key, [item.model_dump() for item in normalized], ttl_seconds=86400)
    return normalized


@router.get("/anime", response_model=Dict[str, List[BaseMediaCard]])
async def get_anime_catalog(
    db: Session = Depends(get_cache_db)
):
    """Returns structured anime hub shelves and a dedicated anime spotlight."""
    cache = CacheEngine(db)
    cache_key = "anime_hub_shelves_v1"
    if cached_data := cache.get(cache_key):
        return cached_data

    trending, popular, top_rated, upcoming = await asyncio.gather(
        fetch_anilist_list_safe("ANIME", ["TRENDING_DESC"]),
        fetch_anilist_list_safe("ANIME", ["POPULAR_DESC"]),
        fetch_anilist_list_safe("ANIME", ["SCORE_DESC"]),
        fetch_anilist_list_safe("ANIME", ["POPULAR_DESC"], status="NOT_YET_RELEASED"),
    )

    # Curate top 6 anime for the hero carousel (high buzz + valid banner)
    spotlight_candidates = trending + popular
    spotlight: List[BaseMediaCard] = []
    seen = set()

    for item in spotlight_candidates:
        if item.id not in seen and item.banner_url and (item.rating or 0) >= 7.0:
            seen.add(item.id)
            spotlight.append(item)
            if len(spotlight) == 6:
                break

    # Fallback to fill up to 5 if rating floor was too strict
    if len(spotlight) < 5:
        for item in spotlight_candidates:
            if item.id not in seen and item.banner_url:
                seen.add(item.id)
                spotlight.append(item)
                if len(spotlight) == 5:
                    break

    payload = {
        "spotlight": spotlight,
        "trending_anime": trending,
        "popular_anime": popular,
        "top_rated_anime": top_rated,
        "upcoming_anime": upcoming,
    }

    cache.set(
        cache_key,
        {k: [item.model_dump() for item in v] for k, v in payload.items()},
        ttl_seconds=21600
    )
    return payload


@router.get("/schedule", response_model=Dict[str, List[ScheduleEntry]])
async def get_release_schedule(
    week_offset: int = Query(0, description="Offset by weeks (0 = current week, 1 = next week, -1 = previous)"),
    tz: str = Query("UTC", description="User's local timezone (e.g., America/Chicago)"),
    db: Session = Depends(get_cache_db),
):
    """Return upcoming anime episodes mapped perfectly to the user's local Monday-Sunday week."""
    try:
        user_tz = zoneinfo.ZoneInfo(tz)
    except Exception:
        user_tz = zoneinfo.ZoneInfo("UTC")

    cache_key = f"anime_schedule_wo_{week_offset}_{tz}"
    cache = CacheEngine(db)
    if cached_data := cache.get(cache_key):
        return cached_data

    # 1. Determine Monday 00:00:00 and Sunday 23:59:59 in the USER'S local timezone
    now_local = datetime.now(user_tz)
    
    # .weekday() returns 0 for Monday, 6 for Sunday
    days_since_monday = now_local.weekday()
    start_of_current_week = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
    
    # 2. Shift the week based on week_offset
    target_start_local = start_of_current_week + timedelta(weeks=week_offset)
    target_end_local = target_start_local + timedelta(days=7) # Next Monday 00:00:00

    # 3. Convert absolute local bounds to UTC timestamps for AniList
    start_ts = int(target_start_local.timestamp())
    end_ts = int(target_end_local.timestamp()) - 1

    # Fetch entries (passing exact timestamps now)
    raw_entries = await fetch_anilist_schedule(start_ts, end_ts)
    
    # 4. Group results into buckets accurately according to the local timezone
    ordered_days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    grouped_payload = {day: [] for day in ordered_days}

    for entry in raw_entries:
        airing_at = entry.get("airing_at")
        if not airing_at:
            continue
            
        # Convert UTC timestamp BACK to user's local timezone to find which day bucket it belongs in
        local_dt = datetime.fromtimestamp(airing_at, tz=timezone.utc).astimezone(user_tz)
        day_name = local_dt.strftime("%A").lower()
        
        if day_name in grouped_payload:
            se = ScheduleEntry(**{**entry, "title": entry.get("title") or "Untitled anime"})
            grouped_payload[day_name].append(se.model_dump())

    cache.set(cache_key, grouped_payload, ttl_seconds=900)
    return grouped_payload

@router.get("/media/{media_id}", response_model=DetailedMedia)
async def get_media_details(
    media_id: str, 
    db: Session = Depends(get_cache_db),
    map_db: Session = Depends(get_mapping_db)
):
    prefix = ''.join([c for c in media_id if not c.isdigit()])
    raw_id_str = ''.join([c for c in media_id if c.isdigit()])
    if not raw_id_str: raise HTTPException(status_code=400, detail="Invalid ID")
    raw_id = int(raw_id_str)
    
    cache = CacheEngine(db)
    cache_key = f"details_{media_id}"
    if cached_data := cache.get(cache_key): return cached_data

    mapper = MappingEngine(map_db)
    mapped_ids = mapper.get_all_ids(media_id) or {}
    
    detailed_model = None

    if prefix in ["tt", "tm"] or (prefix in ["a", "m"] and mapped_ids.get("tmdb_tv_id")):
        actual_prefix = "tt" if (prefix == "tt" or mapped_ids.get("tmdb_tv_id")) else "tm"
        actual_raw_id = mapped_ids.get("tmdb_tv_id") if mapped_ids.get("tmdb_tv_id") else raw_id
        is_tv = (actual_prefix == "tt")
        
        data = await get_tmdb_details(actual_raw_id, is_tv)
        if not data: raise HTTPException(status_code=404, detail="TMDB Media not found")
        
        release_date = data.get("release_date") or data.get("first_air_date") or ""
        videos = data.get("videos", {}).get("results", [])
        trailer = next((v["key"] for v in videos if v["type"] == "Trailer" and v["site"] == "YouTube"), None)
        logos = data.get("images", {}).get("logos", [])
        best_logo = next((l for l in logos if l.get("iso_639_1") == "en"), logos[0] if logos else None)
        
        status_map = {
            "Returning Series": MediaStatus.RELEASING, "In Production": MediaStatus.NOT_YET_AIRED,
            "Ended": MediaStatus.FINISHED, "Canceled": MediaStatus.CANCELLED, "Released": MediaStatus.FINISHED
        }

        # --- RICH METADATA EXTRACTION ---
        runtime = data.get("runtime") or (data.get("episode_run_time")[0] if data.get("episode_run_time") else None)
        
        # Age Rating
        age_rating = None
        if is_tv:
            cr = data.get("content_ratings", {}).get("results", [])
            age_rating = next((r["rating"] for r in cr if r["iso_3166_1"] == "US"), None)
        else:
            rd = data.get("release_dates", {}).get("results", [])
            us_release = next((r for r in rd if r["iso_3166_1"] == "US"), None)
            if us_release:
                age_rating = next((d["certification"] for d in us_release.get("release_dates", []) if d.get("certification")), None)
        
        # Cast
        cast_raw = data.get("credits", {}).get("cast", [])[:10]
        cast_members = [
            CastMember(name=c["name"], character=c.get("character", ""), profile_url=f"{IMG_BASE}{c['profile_path']}" if c.get("profile_path") else None) 
            for c in cast_raw
        ]
        
        # Director
        director = None
        if is_tv:
            creators = data.get("created_by", [])
            director = creators[0]["name"] if creators else None
        else:
            crew = data.get("credits", {}).get("crew", [])
            director_obj = next((c for c in crew if c["job"] == "Director"), None)
            director = director_obj["name"] if director_obj else None
            
        # Recommendations (Merge Recommendations + Similar to ensure we have enough)
        recs_raw = data.get("recommendations", {}).get("results", [])
        if len(recs_raw) < 5:
            recs_raw.extend(data.get("similar", {}).get("results", []))
            
        for r in recs_raw:
            r["type"] = "tv" if is_tv else "movie"
            r["id"] = f"{actual_prefix}{r['id']}" # Ensure ids have standard prefix
        recommendations = normalize_list(recs_raw)[:12]

        seasons = []
        tmdb_episodes = []

        # Determine if this TMDB entry is confirmed anime via the mapping DB.
        # If it has ANY MAL mapping, it's anime — get the root (season 1) MAL id as fallback.
        is_confirmed_anime = bool(mapped_ids and mapped_ids.get("mal_id"))
        root_mal_id = mapped_ids.get("mal_id") if is_confirmed_anime else None

        if is_tv:
            seasons_data, tmdb_episodes = await get_tmdb_seasons_and_episodes(actual_raw_id, data.get("seasons", []))
            
            for s in seasons_data:
                s_num = s["season_number"]
                season_mapped_id = f"tt{actual_raw_id}"
                exact_kitsu = None
                
                if prefix in ["m", "a"] or mapped_ids.get("tmdb_tv_id") or is_confirmed_anime:
                    exact_anilist = mapper.get_anilist_id_for_tmdb_season(actual_raw_id, s_num)
                    exact_mal = mapper.get_mal_id_for_tmdb_season(actual_raw_id, s_num)
                    
                    if exact_anilist:
                        season_mapped_id = f"a{exact_anilist}"
                        exact_kitsu = mapper.get_kitsu_id_from_anilist(exact_anilist)
                    elif exact_mal:
                        season_mapped_id = f"m{exact_mal}"
                        exact_kitsu = mapper.get_kitsu_id_from_mal(exact_mal)
                    elif is_confirmed_anime and mapped_ids.get("anilist_id"):
                        season_mapped_id = f"a{mapped_ids['anilist_id']}"
                        exact_kitsu = mapper.get_kitsu_id_from_anilist(mapped_ids['anilist_id']) or mapped_ids.get("kitsu_id")
                    elif is_confirmed_anime and root_mal_id:
                        # No Fribb entry for this season — fall back to the root Anime/MAL ID so the
                        # resolver still routes to AniDB (absolute episode numbers ensure correctness).
                        season_mapped_id = f"m{root_mal_id}"
                        exact_kitsu = mapper.get_kitsu_id_from_mal(root_mal_id)

                seasons.append(Season(
                    season_number=s_num, title=s["title"], episode_count=s["episode_count"],
                    poster_url=s["poster_url"], mapped_id=season_mapped_id
                ))

                season_eps = [ep for ep in tmdb_episodes if ep.season_number == s_num]
                for ep in season_eps:
                    ep.mapped_id = season_mapped_id
                    
                if exact_kitsu:
                    await enrich_episodes_with_kitsu(season_eps, exact_kitsu)

        # Determine final media type: upgrade tv → anime when mapping DB confirms it
        if is_tv and is_confirmed_anime:
            final_media_type = MediaType.ANIME
        elif is_tv:
            final_media_type = MediaType.TV
        else:
            final_media_type = MediaType.MOVIE

        detailed_model = DetailedMedia(
            id=media_id, title=data.get("title") or data.get("name"),
            type=final_media_type,
            poster_url=f"{IMG_BASE}{data.get('poster_path')}" if data.get('poster_path') else None,
            banner_url=f"https://image.tmdb.org/t/p/w1280{data.get('backdrop_path')}" if data.get('backdrop_path') else None,
            release_year=int(release_date.split("-")[0]) if release_date else None, rating=round(data.get("vote_average", 0), 1),
            description=data.get("overview"), genres=[g["name"] for g in data.get("genres", [])],
            status=status_map.get(data.get("status", "Ended"), MediaStatus.FINISHED), studios=[c["name"] for c in data.get("production_companies", [])],
            clear_logo_url=f"{IMG_BASE}{best_logo['file_path']}" if best_logo else None, trailer_url=f"https://www.youtube.com/watch?v={trailer}" if trailer else None,
            seasons=seasons, episodes=tmdb_episodes,
            # Assigned Rich Data
            tagline=data.get("tagline"), runtime=runtime, age_rating=age_rating,
            director=director, cast=cast_members, recommendations=recommendations
        )

    elif prefix in ["a", "m"]:
        anilist_data = await get_anilist_details(anilist_id=raw_id if prefix == "a" else None, mal_id=raw_id if prefix == "m" else None)
        if not anilist_data: raise HTTPException(status_code=404, detail="Anime not found")
        
        english_title = anilist_data.get("title", {}).get("english") or anilist_data.get("title", {}).get("romaji")
        mal_id = anilist_data.get("idMal")
        resolved_id = f"a{anilist_data.get('id')}"

        # Fetch synopsis and the season chain (see get_anilist_season_chain)
        # concurrently. The chain is what fixes multi-cour anime with no
        # TMDB tv mapping: AniList stores "Season 2" as a totally separate
        # media id, so without this every anime would show as one season.
        jikan_details, season_chain = await asyncio.gather(
            get_jikan_details(mal_id) if mal_id else asyncio.sleep(0),
            get_anilist_season_chain(anilist_data.get("id"))
        )

        desc = jikan_details.get("synopsis") if jikan_details else anilist_data.get("description")
        clean_desc = re.sub('<[^<]+>', '', desc) if desc else ""
        trailer_data = anilist_data.get("trailer", {})
        
        status_map = {"RELEASING": MediaStatus.RELEASING, "FINISHED": MediaStatus.FINISHED, "NOT_YET_RELEASED": MediaStatus.NOT_YET_AIRED}

        # --- RICH METADATA EXTRACTION ---
        runtime = anilist_data.get("duration")
        age_rating = jikan_details.get("rating") if jikan_details else None

        # Cast
        cast_edges = anilist_data.get("characters", {}).get("edges", [])
        cast_members = [
            CastMember(name=e["node"]["name"]["full"], character=e.get("role", "Unknown"), profile_url=e["node"]["image"]["large"])
            for e in cast_edges if e.get("node")
        ]

        # Director
        staff_edges = anilist_data.get("staff", {}).get("edges", [])
        director_edge = next((e for e in staff_edges if "Director" in e.get("role", "")), None)
        director = director_edge["node"]["name"]["full"] if director_edge else None

        # Recommendations
        recs_nodes = anilist_data.get("recommendations", {}).get("nodes", [])
        recs_raw = []
        for node in recs_nodes:
            rm = node.get("mediaRecommendation")
            if not rm: continue
            rec_type = rm.get("type", "ANIME")
            prefix_rec = "mg" if rec_type == "MANGA" else "a"
            recs_raw.append({
                "id": f"{prefix_rec}{rm['id']}",
                "title": rm.get("title", {}).get("english") or rm.get("title", {}).get("romaji"),
                "poster_url": rm.get("coverImage", {}).get("extraLarge"),
                "banner_url": rm.get("bannerImage"),
                "type": "manga" if rec_type == "MANGA" else "anime",
                "release_year": rm.get("seasonYear"),
                "rating": round(rm.get("averageScore", 0) / 10, 1) if rm.get("averageScore") else None
            })
        recommendations = normalize_list(recs_raw)[:12]

        # --- SEASON STITCHING ---
        # If the relations walk found more than just the starting node, build
        # one Season + episode block per chain entry (each resolved through
        # its own mal_id -> kitsu_id). Otherwise fall back to the old
        # single-season behavior.
        seasons: List[Season] = []
        all_episodes: List[Any] = []

        if len(season_chain) > 1:
            kitsu_lookup = {
                s["anilist_id"]: (mapper.get_kitsu_id_from_mal(s["mal_id"]) if s.get("mal_id") else None)
                for s in season_chain
            }
            ep_tasks = [
                get_kitsu_episodes(kitsu_lookup[s["anilist_id"]], s.get("mal_id"), f"a{s['anilist_id']}")
                if kitsu_lookup.get(s["anilist_id"]) else asyncio.sleep(0)
                for s in season_chain
            ]
            season_episode_lists = await asyncio.gather(*ep_tasks)

            for idx, (s, eps) in enumerate(zip(season_chain, season_episode_lists), start=1):
                eps = eps if isinstance(eps, list) else []
                season_mapped_id = f"a{s['anilist_id']}" if s.get("anilist_id") else f"m{s['mal_id']}"
                for ep in eps:
                    ep.season_number = idx
                    ep.mapped_id = season_mapped_id
                seasons.append(Season(
                    season_number=idx,
                    title=s.get("title") or f"Season {idx}",
                    episode_count=s.get("episodes") or len(eps),
                    poster_url=None,
                    mapped_id=season_mapped_id
                ))
                all_episodes.extend(eps)
        else:
            kitsu_id = mapped_ids.get("kitsu_id")
            kitsu_eps = await get_kitsu_episodes(kitsu_id, mal_id, resolved_id) if kitsu_id else []
            all_episodes = kitsu_eps if isinstance(kitsu_eps, list) else []
            seasons = [Season(
                season_number=1, title=english_title,
                episode_count=len(all_episodes), poster_url=None, mapped_id=resolved_id
            )]

        detailed_model = DetailedMedia(
            id=resolved_id,
            title=english_title,
            type=MediaType.ANIME,
            poster_url=anilist_data.get("coverImage", {}).get("extraLarge"),
            banner_url=anilist_data.get("bannerImage"),
            release_year=anilist_data.get("seasonYear"),
            rating=round(anilist_data.get("averageScore") / 10, 1) if anilist_data.get("averageScore") else None,
            description=clean_desc,
            genres=anilist_data.get("genres", []),
            studios=[s["name"] for s in anilist_data.get("studios", {}).get("nodes", [])],
            status=status_map.get(anilist_data.get("status"), MediaStatus.FINISHED),
            clear_logo_url=None,
            trailer_url=f"https://www.youtube.com/watch?v={trailer_data.get('id')}" if trailer_data and trailer_data.get("site") == "youtube" else None,
            seasons=seasons,
            episodes=all_episodes,
            # Assigned Rich Data
            tagline=None, runtime=runtime, age_rating=age_rating,
            director=director, cast=cast_members, recommendations=recommendations
        )

    if detailed_model:
        cache.set(cache_key, detailed_model.model_dump(), ttl_seconds=86400)
    return detailed_model