from typing import Optional, Dict, List, Set

from sqlalchemy.orm import Session
from app.core.db_models import AnimeMapping


class MappingEngine:
    """
    Translates Custom IDs (tt, tm, m, a, k) back and forth using the mapping.db
    """

    def __init__(self, db: Session):
        self.db = db

    def extract_id(self, custom_id: str) -> int:
        """Strips non-digit characters and returns the raw integer ID."""
        raw_num = ''.join([c for c in custom_id if c.isdigit()])
        return int(raw_num) if raw_num else 0

    def extract_prefix(self, custom_id: str) -> str:
        """Extracts the prefix identifier from custom ID string."""
        prefix = ''.join([c for c in custom_id if not c.isdigit()]).lower().strip('_')
        return prefix

    def get_all_ids(self, custom_id: str) -> Optional[Dict[str, Optional[int]]]:
        """
        Given ANY custom ID (e.g. 'tt127532', 'm58567', 'a16498', 'mal_58567'),
        returns a dictionary of ALL associated IDs for that show.
        """
        prefix = self.extract_prefix(custom_id)
        raw_id = self.extract_id(custom_id)
        if not raw_id:
            return None

        query = self.db.query(AnimeMapping)

        if prefix in ["m", "mal"]:
            mapping = query.filter(AnimeMapping.mal_id == raw_id).first()
        elif prefix in ["a", "anilist"]:
            mapping = query.filter(AnimeMapping.anilist_id == raw_id).first()
        elif prefix in ["k", "kitsu"]:
            mapping = query.filter(AnimeMapping.kitsu_id == raw_id).first()
        elif prefix in ["tt"]:
            mapping = query.filter(AnimeMapping.tmdb_tv_id == raw_id).first()
        elif prefix in ["tm"]:
            mapping = query.filter(AnimeMapping.tmdb_movie_id == raw_id).first()
        else:
            return None

        if not mapping:
            return None

        return {
            "mal_id": mapping.mal_id,
            "anilist_id": mapping.anilist_id,
            "tmdb_tv_id": mapping.tmdb_tv_id,
            "tmdb_movie_id": mapping.tmdb_movie_id,
            "kitsu_id": mapping.kitsu_id,
            "tvdb_id": mapping.tvdb_id,
            "tmdb_season": mapping.tmdb_season,
        }

    def convert_tmdb_episode_to_absolute(self, custom_id: str, season: int, episode: int) -> int:
        """
        Calculates absolute episode number for anime seasons.
        """
        return episode
    
    def get_mal_id_for_tmdb_season(self, tmdb_tv_id: int, season_number: int) -> Optional[int]:
        """
        Finds the specific MAL ID for a specific TMDB season.
        """
        # Exact match for the season
        mapping = self.db.query(AnimeMapping).filter(
            AnimeMapping.tmdb_tv_id == tmdb_tv_id,
            AnimeMapping.tmdb_season == season_number,
            AnimeMapping.mal_id.isnot(None)
        ).first()

        if mapping and mapping.mal_id:
            return mapping.mal_id

        # Season 1 fallback (either explicit season 1 or unstated season)
        if season_number == 1:
            base_mapping = self.db.query(AnimeMapping).filter(
                AnimeMapping.tmdb_tv_id == tmdb_tv_id,
                AnimeMapping.mal_id.isnot(None)
            ).first()
            if base_mapping and base_mapping.mal_id:
                return base_mapping.mal_id
                
        return None

    def get_anilist_id_for_tmdb_season(self, tmdb_tv_id: int, season_number: int) -> Optional[int]:
        """
        Finds the specific AniList ID for a specific TMDB season.
        """
        mapping = self.db.query(AnimeMapping).filter(
            AnimeMapping.tmdb_tv_id == tmdb_tv_id,
            AnimeMapping.tmdb_season == season_number,
            AnimeMapping.anilist_id.isnot(None)
        ).first()

        if mapping and mapping.anilist_id:
            return mapping.anilist_id

        if season_number == 1:
            base_mapping = self.db.query(AnimeMapping).filter(
                AnimeMapping.tmdb_tv_id == tmdb_tv_id,
                AnimeMapping.anilist_id.isnot(None)
            ).first()
            if base_mapping and base_mapping.anilist_id:
                return base_mapping.anilist_id
                
        return None
    
    def get_kitsu_id_from_mal(self, mal_id: int) -> Optional[int]:
        """Gets the Kitsu ID from MAL ID using the mapping database."""
        mapping = self.db.query(AnimeMapping).filter(
            AnimeMapping.mal_id == mal_id,
            AnimeMapping.kitsu_id.isnot(None)
        ).first()
        return mapping.kitsu_id if mapping else None

    def get_kitsu_id_from_anilist(self, anilist_id: int) -> Optional[int]:
        """Gets the Kitsu ID from AniList ID using the mapping database."""
        mapping = self.db.query(AnimeMapping).filter(
            AnimeMapping.anilist_id == anilist_id,
            AnimeMapping.kitsu_id.isnot(None)
        ).first()
        return mapping.kitsu_id if mapping else None

    # ==========================================
    # BATCH ANIME-DETECTION LOOKUPS
    # ==========================================

    def get_anime_tmdb_tv_ids(self, tmdb_tv_ids: List[int]) -> Set[int]:
        """Given a batch of TMDB TV IDs, returns the subset that are indexed as anime."""
        if not tmdb_tv_ids:
            return set()
        rows = self.db.query(AnimeMapping.tmdb_tv_id).filter(
            AnimeMapping.tmdb_tv_id.in_(tmdb_tv_ids),
            AnimeMapping.tmdb_tv_id.isnot(None)
        ).all()
        return {r[0] for r in rows if r[0] is not None}

    def get_anime_tmdb_movie_ids(self, tmdb_movie_ids: List[int]) -> Set[int]:
        """Given a batch of TMDB Movie IDs, returns the subset that are indexed as anime."""
        if not tmdb_movie_ids:
            return set()
        rows = self.db.query(AnimeMapping.tmdb_movie_id).filter(
            AnimeMapping.tmdb_movie_id.in_(tmdb_movie_ids),
            AnimeMapping.tmdb_movie_id.isnot(None)
        ).all()
        return {r[0] for r in rows if r[0] is not None}