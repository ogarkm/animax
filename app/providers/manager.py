import os
import importlib
import inspect
import asyncio
from pathlib import Path
from typing import List, Dict, Optional
import httpx

from app.providers.base import BaseProvider
from app.models.player import SourceOffer, MediaContext
from app.providers.metadata.anilist import get_anilist_details
from app.providers.metadata.tmdb import get_tmdb_details


from app.core.database import MappingSessionLocal
from app.services.mapping_engine import MappingEngine


def _matches_type(provider_type: str, requested: str) -> bool:
    """Standardizes and matches provider types against requested media types."""
    if requested in ("all", None, ""):
        return True
    if provider_type == requested:
        return True
    if provider_type == "movie_tv" and requested in ("movie", "tv", "movie_tv", "anime"):
        return True
    return False


class ProviderManager:
    def __init__(self):
        self.providers: Dict[str, BaseProvider] = {}
        self.client: httpx.AsyncClient = httpx.AsyncClient(timeout=15.0)
        self._load_providers()

    def _load_providers(self):
        """Dynamically loads all scraper classes from the provider folders reliably across OSes."""
        base_paths = {
            "anime": Path("app/providers/anime"),
            "movies": Path("app/providers/movies_tv")
        }

        for p_type, folder in base_paths.items():
            if not folder.exists():
                continue
                
            for file in folder.glob("*.py"):
                if file.name == "__init__.py":
                    continue

                # Cross-platform module path conversion using pathlib.parts
                module_path = ".".join(file.with_suffix("").parts)
                try:
                    module = importlib.import_module(module_path)
                except Exception as e:
                    print(f"[ProviderManager] Failed to import module {module_path}: {e}")
                    continue
                
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    # Find classes that inherit from BaseProvider (but aren't BaseProvider itself)
                    if issubclass(obj, BaseProvider) and obj is not BaseProvider:
                        instance = obj(client=self.client)
                        self.providers[instance.name.lower()] = instance
                        print(f"[ProviderManager] Loaded {p_type} scraper: {instance.name}")

    async def _resolve_media_context(self, mapped_id: str) -> Optional[MediaContext]:
        """Resolves metadata context once before broadcasting to all scrapers."""
        if not mapped_id:
            return None

        raw_num = ''.join([c for c in mapped_id if c.isdigit()])
        if not raw_num:
            return None
        raw_id = int(raw_num)
        prefix = ''.join([c for c in mapped_id if not c.isdigit()]).lower()

        primary_title = ""
        romaji_title = None
        english_title = None
        synonyms = []
        year = None
        mal_id = None
        anilist_id = None
        tmdb_tv_id = None
        tmdb_movie_id = None

        # Resolve mapped IDs using MappingEngine
        try:
            db = MappingSessionLocal()
            engine = MappingEngine(db)
            all_ids = engine.get_all_ids(mapped_id)
            if all_ids:
                mal_id = all_ids.get("mal_id")
                anilist_id = all_ids.get("anilist_id")
                tmdb_tv_id = all_ids.get("tmdb_tv_id")
                tmdb_movie_id = all_ids.get("tmdb_movie_id")
            db.close()
        except Exception as e:
            print(f"[ProviderManager] Mapping lookup error for {mapped_id}: {e}")

        try:
            if prefix in ["a", "anilist"]:
                data = await get_anilist_details(anilist_id=raw_id)
                if data:
                    title_dict = data.get("title", {})
                    english_title = title_dict.get("english")
                    romaji_title = title_dict.get("romaji")
                    primary_title = english_title or romaji_title or title_dict.get("native", "")
                    year = data.get("seasonYear")
                    synonyms = [t for t in [romaji_title, english_title, title_dict.get("native")] if t and t != primary_title]
            elif prefix in ["m", "mal"]:
                data = await get_anilist_details(mal_id=raw_id)
                if data:
                    title_dict = data.get("title", {})
                    english_title = title_dict.get("english")
                    romaji_title = title_dict.get("romaji")
                    primary_title = english_title or romaji_title or ""
                    year = data.get("seasonYear")
                    synonyms = [t for t in [romaji_title, english_title] if t and t != primary_title]
                else:
                    resp = await self.client.get(f"https://api.jikan.moe/v4/anime/{raw_id}")
                    if resp.status_code == 200:
                        jdata = resp.json().get("data", {})
                        english_title = jdata.get("title_english")
                        primary_title = english_title or jdata.get("title") or ""
                        year = jdata.get("year")
                        synonyms = [jdata.get("title")] if jdata.get("title") and jdata.get("title") != primary_title else []
            elif prefix in ["tt", "tm"]:
                is_tv = (prefix == "tt")
                data = await get_tmdb_details(raw_id, is_tv=is_tv)
                if data:
                    primary_title = data.get("name") if is_tv else data.get("title", "")
                    date_str = data.get("first_air_date") if is_tv else data.get("release_date")
                    year = int(date_str[:4]) if date_str and len(date_str) >= 4 else None
        except Exception as e:
            print(f"[ProviderManager] Context Resolution Warning for {mapped_id}: {e}")

        if not primary_title:
            primary_title = mapped_id

        return MediaContext(
            mapped_id=mapped_id,
            primary_title=primary_title,
            romaji_title=romaji_title,
            english_title=english_title,
            synonyms=synonyms,
            year=year,
            mal_id=mal_id,
            anilist_id=anilist_id,
            tmdb_tv_id=tmdb_tv_id,
            tmdb_movie_id=tmdb_movie_id
        )

    async def get_all_source_offers(
        self, 
        mapped_id: str, 
        episode: int, 
        media_type: str, 
        is_dub: bool,
        context: Optional[MediaContext] = None
    ) -> List[SourceOffer]:
        """
        Fires all relevant scrapers concurrently.
        Resolves media context once beforehand if not already provided.
        """
        if context is None:
            context = await self._resolve_media_context(mapped_id)

        tasks = []
        for name, provider in self.providers.items():
            if _matches_type(provider.provider_type, media_type):
                tasks.append(self._safe_get_offers(provider, mapped_id, episode, is_dub, context))

        results = await asyncio.gather(*tasks)
        all_offers = []
        for offer_list in results:
            if offer_list:
                all_offers.extend(offer_list)

        return all_offers

    async def _safe_get_offers(
        self, 
        provider: BaseProvider, 
        mapped_id: str, 
        episode: int, 
        is_dub: bool,
        context: Optional[MediaContext]
    ) -> List[SourceOffer]:
        try:
            return await provider.get_source_offers(mapped_id, episode, is_dub, context=context)
        except Exception as e:
            print(f"[Provider Error] {provider.name} failed to get offers: {e}")
            return []

    def get_provider(self, name: str) -> Optional[BaseProvider]:
        return self.providers.get(name.lower())


# Singleton instance to be used across the app
provider_manager = ProviderManager()