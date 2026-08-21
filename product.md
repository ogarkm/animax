This is the **Animax Master Blueprint**. 

This document outlines the entire backend architecture, leaving absolutely no detail unturned. It covers the exact folder structure, the split SQLite database schemas, the Unified Pydantic Models, the Mapping Engine logic, the Provider architecture, and the Controller flows. 

Save this document. This is your bible for building the backend.

---

# PART 1: The Project Directory Structure
To keep a massive aggregator manageable, we strictly separate Data (Models), Logic (Services/Providers), and Presentation (Routers/API endpoints).

```text
animax-backend/
├── app/
│   ├── main.py                     # FastAPI entry point, lifespan events (startup/shutdown)
│   ├── core/                       # Core system configurations
│   │   ├── config.py               # Environment variables (JWT secrets, port, etc.)
│   │   ├── database.py             # SQLite engine setup, WAL mode configuration
│   │   └── security.py             # JWT token generation, bcrypt password hashing
│   ├── models/                     # Pydantic schemas (Unified Adapter Models)
│   │   ├── media.py                # BaseMediaCard, DetailedMedia, Season, Episode
│   │   ├── player.py               # SourceOffer, PlayerStateModel, Track, SkipTime
│   │   └── user.py                 # UserProfile, WatchProgress, Collection
│   ├── databases/                  # The physical SQLite database files (gitignored)
│   │   ├── users.db                # User state
│   │   ├── mapping.db              # Fribb JSON mappings
│   │   └── cache.db                # Metadata, API responses, Scraper health logs
│   ├── routers/                    # FastAPI Endpoints (The Controllers)
│   │   ├── auth.py                 # /auth/login, /auth/register
│   │   ├── discovery.py            # /api/home, /api/media/{id}, /api/search
│   │   ├── resolver.py             # /api/sources/{id}, /api/player/payload
│   │   └── user.py                 # /api/user/progress, /api/user/collections
│   ├── services/                   # Heavy lifting logic
│   │   ├── mapping_engine.py       # Interacts with mapping.db to resolve TMDB <-> MAL
│   │   ├── cache_engine.py         # Smart Caching (Dynamic TTL) logic
│   │   └── auth_service.py         # JWT validation dependencies
│   ├── providers/                  # The Scraper Modules
│   │   ├── base.py                 # BaseProvider class definition
│   │   ├── manager.py              # ProviderManager (Auto-discovers and routes scrapers)
│   │   ├── anime/                  # AnimeKai, HiAnime, Gogo classes
│   │   ├── movies_tv/              # FlixHQ, Vidsrc classes
│   │   └── metadata/               # TMDB, AniList (GraphQL), Kitsu, AniSkip fetchers
│   └── workers/                    # Background tasks
│       └── background_jobs.py      # Fribb JSON downloader, Cache cleaner
├── requirements.txt
└── .env
```

---

# PART 2: Database Architecture (Split SQLite w/ WAL)
To prevent "database is locked" errors and ensure lightning-fast read/write speeds, we split the databases and execute `PRAGMA journal_mode=WAL;` on startup.

### 1. `users.db` (Write-Heavy)
Handles everything tied to a specific user.
*   **Table: `Users`** -> `id`, `username`, `password_hash`, `avatar_url`, `created_at`.
*   **Table: `WatchProgress`** -> `user_id`, `internal_media_id`, `episode_number`, `timestamp`, `duration`, `updated_at`. 
    *   *Constraint:* UNIQUE index on `(user_id, internal_media_id, episode_number)` to allow `UPSERT` (overwrite timestamp instead of adding new rows).
*   **Table: `Collections`** -> `user_id`, `internal_media_id`, `status` (watching, completed, dropped), `rating`.

### 2. `mapping.db` (Read-Heavy, Indexed)
Powered by Fribb's JSON. Refreshed via a background job once a week.
*   **Table: `AnimeMappings`** -> `mal_id`, `anilist_id`, `tmdb_tv_id`, `tmdb_movie_id`, `kitsu_id`, `tvdb_id`, `tvdb_season`, `tmdb_season`.
    *   *Indexing:* We place B-Tree indexes on `mal_id`, `anilist_id`, and `tmdb_tv_id`. This allows a 0.001ms lookup when mapping TMDB to MAL.

### 3. `cache.db` (High-Turnover)
Replaces Redis. Uses an SQLite table designed for expiring rows.
*   **Table: `MetadataCache`** -> `cache_key` (e.g., `details_tmdb_123`), `json_data` (BLOB/Text), `expires_at` (Unix Timestamp).
*   **Table: `ScraperHealth`** -> `provider_name`, `endpoint`, `success_count`, `failure_count`, `last_failed_at`. (Used by ProviderManager to penalize failing scrapers).

---

# PART 3: Unified Pydantic Models (The Adapter Standard)
These are the exact data structures your frontend will consume. No matter what external API is used, the frontend receives *this* exact format.

### 1. Media Models (Discovery Layer)
```python
class BaseMediaCard(BaseModel):
    id: str                 # Unified prefixed ID: 'tmdb_tv_1399' or 'mal_anime_58567'
    title: str
    poster_url: str
    banner_url: Optional[str]
    type: str               # 'movie', 'tv', 'anime' (Manga/LiveTV later)
    release_year: int
    rating: float           # Normalized to 10.0 scale

class EpisodeShort(BaseModel):
    id: str                 # Internal episode ID (e.g., 'tmdb_tv_1399_s1_e1')
    absolute_number: int    # Critical for anime mapping (e.g., Ep 13)
    season_number: int      # 1 for TV, mostly 1 for anime
    episode_number: int     # Relative to season
    title: str
    thumbnail_url: Optional[str]
    is_filler: bool         # Mapped from AnimeFillerList/AniList

class DetailedMedia(BaseMediaCard):
    description: str
    genres: List[str]
    studios: List[str]
    status: str             # 'RELEASING', 'FINISHED', 'NOT_YET_AIRED'
    clear_logo_url: Optional[str]
    episodes: List[EpisodeShort] # One massive list. Frontend UI decides how to group by season.
```

### 2. Resolver & Player Models (Player Layer)
```python
class SourceOffer(BaseModel):
    provider: str           # e.g., 'hianime', 'flixhq'
    type: str               # 'iframe' or 'internal'
    quality: str            # '1080p', 'auto', '4k'
    dub: bool               # True if dub, False if sub
    url: str                # Direct iframe URL, OR internal /api/player/payload trigger URL

class SkipTime(BaseModel):
    type: str               # 'intro', 'outro', 'recap'
    start: float            # in seconds
    end: float

class Track(BaseModel):
    file: str               # .vtt url
    label: str              # 'English', 'Spanish'
    kind: str               # 'captions', 'thumbnails'
    default: bool

class PlayerStateModel(BaseModel):
    stream_url: str         # The master .m3u8 or .mp4
    headers: Dict[str, str] # e.g., {"Referer": "https://megacloud.club/"}
    tracks: List[Track]
    skips: List[SkipTime]
    
    # Metadata for the Player UI
    media_title: str
    episode_title: str
    next_episode_id: Optional[str] # URL or ID to fetch the next payload without reloading
```

---

# PART 4: The Mapping Engine (Handling Fribb)
Anime tracking is notoriously awful because TMDB maps Anime by Seasons (e.g., Attack on Titan Season 4 Part 2), while MAL/AniList maps them as entirely separate shows.

**The Mapping Flow (`mapping_engine.py`):**
1. User clicks "Attack on Titan S4 P2" (TMDB ID `1429` Season `4`).
2. The Controller queries `mapping.db`: `SELECT mal_id, anilist_id FROM AnimeMappings WHERE tmdb_tv_id = 1429 AND tmdb_season = 4`.
3. The DB returns MAL ID `48583`.
4. The Backend now knows: "To get the thumbnail, use TMDB. To get the stream, tell the Anime Providers to search for MAL ID 48583. To get the skip times, ask AniSkip for MAL ID 48583."

*This entirely abstracts the mapping chaos away from your frontend.*

---

# PART 5: The Provider Architecture (Plug & Play)

### The `BaseProvider` Interface
Every scraper inherits from this. It enforces structure.
```python
class BaseProvider:
    name: str
    provider_type: str # 'anime', 'movie', 'tv'
    
    async def search(self, title: str, release_year: int) -> str:
        # Returns internal provider ID
        pass

    async def get_episodes(self, provider_id: str) -> List[dict]:
        # Returns list of episode IDs mapped to absolute episode numbers
        pass

    async def extract_stream(self, episode_id: str, server_name: str) -> dict:
        # Returns {stream_url: str, tracks: list, headers: dict}
        pass
```

### The `ProviderManager` (`manager.py`)
On FastAPI startup, this class runs `os.listdir('providers/anime')` and imports every python class that inherits from `BaseProvider`.
*   **The Fallback Chain:** When Endpoint 1 requests sources, `ProviderManager` uses `asyncio.gather()` to hit AnimeKai, HiAnime, and Gogo concurrently. It applies a timeout (e.g., 5 seconds). Whichever providers respond in time are formatted into `SourceOffer` models and sent to the user.
*   **Health Tracking:** If `HiAnime.extract_stream()` throws an error 5 times in an hour, the `ProviderManager` marks it as `degraded` and drops it to the bottom of the fallback chain, preventing user slowdowns.

---

# PART 6: Smart Caching Engine (Dynamic TTL)
Located in `cache_engine.py`. Hits the `MetadataCache` table before making external requests.

**TTL (Time-to-Live) Rules Engine:**
*   **Rule 1 (Finished Content):** If TMDB/AniList returns `status: "FINISHED"`, hash the response and cache it for **30 Days**.
*   **Rule 2 (Airing Content):** If `status: "RELEASING"`, cache it for **12 Hours**. (Advanced addition: check the `nextAiringEpisode` field from AniList GraphQL. Set the cache to expire exactly 1 hour after the episode drops).
*   **Rule 3 (Source Links):** M3U8 links from scrapers expire rapidly due to IP-locking or tokens. Cache `PlayerStateModel` for **1 Hour maximum**, or don't cache it at all depending on the provider.
*   **Rule 4 (Search Results):** Cache query `search_avatar_the_last_airbender` for **7 Days**.

---

# PART 7: API Endpoints (The FastAPI Routers)

### 1. Discovery (`/routers/discovery.py`)
*   `GET /api/home`
    *   *Action:* Fetches TMDB Trending, TMDB Popular Movies, AniList Trending Anime.
    *   *Process:* Runs concurrently. Maps to `BaseMediaCard`. Cached for 24 hours.
*   `GET /api/media/{id}` (e.g., `tmdb_tv_123` or `mal_anime_456`)
    *   *Action:* Fetches full details.
    *   *Process:* Uses Mapping Engine to identify media type. If Anime, queries AniList GraphQL. If TV, queries TMDB. Formats `DetailedMedia` with the massive unified episode array. Cached dynamically.

### 2. The Resolver (`/routers/resolver.py`)
*   `GET /api/sources/{internal_media_id}?episode={abs_num}`
    *   *Action:* Asks "Where can I watch this?"
    *   *Process:* Passes data to `ProviderManager`. Returns `List[SourceOffer]`.
*   `GET /api/player/payload?provider={name}&provider_ep_id={id}`
    *   *Action:* Asks "Give me the video data."
    *   *Process:*
        1. Calls `provider.extract_stream()`.
        2. Async fetch to AniSkip API (using MAL ID).
        3. Async fetch to Kitsu/TMDB for episode thumbnail & synopsis.
        4. Merges into `PlayerStateModel` and returns JSON.

### 3. User State (`/routers/user.py`)
*   `POST /api/user/progress`
    *   *Action:* Saves watch time.
    *   *Payload:* `{media_id: "mal_123", ep: 2, timestamp: 840, duration: 1400}`
    *   *Process:* Requires JWT Auth. UPSERTs into `WatchProgress` SQLite table. (Frontend sends this via Debounce every 30 seconds or on pause).
*   `GET /api/user/continue-watching`
    *   *Action:* Populates the top row of the Home Page for logged-in users.
    *   *Process:* Joins `WatchProgress` with `MetadataCache` to return `BaseMediaCard`s with a progress bar percentage attached.

---

# PART 8: Handling Edge Cases & Pitfalls

### 1. The "Decryption API" Timeout Protocol
Since scrapers rely on volatile decryption APIs (like your `enc-dec.app` or `ShadeOfChaos`), we use the `Tenacity` Python library.
*   If `HiAnime` fails to decrypt, `Tenacity` triggers 1 immediate retry.
*   If it fails again, it throws a `ProviderDecryptionError`.
*   The `ProviderManager` catches this, logs it to `ScraperHealth`, and immediately serves the next source. *The frontend user never sees an error screen.*

### 2. Anime "Part 2" Absolute Episode Mapping
*Problem:* Attack on Titan Season 4 Part 2 is "Episode 1" on TMDB S4, but "Episode 76" on HiAnime.
*Solution:* This is why `EpisodeShort` model has `absolute_number`. When generating the Detailed Page, the backend calculates the absolute episode number by summing previous seasons. When asking the Resolver for a stream, it *always* asks the scraper for `absolute_number: 76`, entirely ignoring TMDB's season structure.

### 3. Cross-Origin Resource Sharing (CORS) & Referers
*Problem:* Custom HTML5 players cannot play Megacloud `.m3u8` files without a specific referer. Browsers block you from faking the `Referer` header in JS.
*Solution:* Two options.
1. (Frontend) Use a library like `Vidstack` which has built-in proxy configurations.
2. (Backend Route) Create a proxy endpoint: `GET /proxy/m3u8?url={target}`. FastAPI fetches the master m3u8 using the correct Python headers, rewrites the internal `.ts` segment URLs to absolute URLs, and serves the clean `.m3u8` directly to your frontend player. (This is advanced, but acts as a silver bullet for CORS).

### 4. Background Workers (The Lifeline)
A heavy backend cannot run Fribb JSON downloads inside a user request.
*   On FastAPI startup, we initialize `APScheduler`.
*   Every Sunday at 3:00 AM, a background task downloads `anime-list-full.json`, parses the 340k lines, and `BULK INSERT OR REPLACE` into `mapping.db`.
*   Every hour, a background task runs `DELETE FROM MetadataCache WHERE expires_at < {current_time}` to prevent your SQLite DB from bloating to 50GB.

---

### Conclusion of the Blueprint

This architecture turns a chaotic scraping project into an enterprise-grade microservice. 
1. The **Adapter Models** guarantee your frontend developer experience is flawless.
2. The **Mapping Engine** bridges the gap between Western TV and Anime.
3. The **Provider Manager** ensures 99% uptime even when sources break.
4. The **Split SQLite WAL DBs** ensure the server runs blazingly fast with minimal RAM.

Your next physical action should be initializing a Git repository, setting up the exact folder structure listed in Part 1, and writing the Pydantic models in `models/media.py`.