import os
import importlib
import inspect
import asyncio
from typing import List, Dict

from app.providers.base import BaseProvider
from app.models.player import SourceOffer

class ProviderManager:
    def __init__(self):
        self.providers: Dict[str, BaseProvider] = {}
        self._load_providers()

    def _load_providers(self):
        """Dynamically loads all scraper classes from the provider folders."""
        base_paths = {
            "anime": "app/providers/anime",
            "movies": "app/providers/movies_tv"
        }

        for p_type, folder_path in base_paths.items():
            if not os.path.exists(folder_path):
                continue
                
            for filename in os.listdir(folder_path):
                if filename.endswith(".py") and filename != "__init__.py":
                    module_name = f"{folder_path.replace('/', '.')}.{filename[:-3]}"
                    module = importlib.import_module(module_name)
                    
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        # Find classes that inherit from BaseProvider (but aren't BaseProvider itself)
                        if issubclass(obj, BaseProvider) and obj is not BaseProvider:
                            instance = obj()
                            self.providers[instance.name.lower()] = instance
                            print(f"[ProviderManager] Loaded {p_type} scraper: {instance.name}")

    async def get_all_source_offers(self, mapped_id: str, episode: int, media_type: str, is_dub: bool) -> List[SourceOffer]:
        """
        Fires all relevant scrapers concurrently.
        Whichever scrapers find the episode return their offers.
        """
        tasks = []
        for name, provider in self.providers.items():
            # Only run anime scrapers for anime, movie scrapers for movies, etc.
            if provider.provider_type == media_type or media_type == "all":
                # Wrap in a safe task so one failing scraper doesn't crash the others
                tasks.append(self._safe_get_offers(provider, mapped_id, episode, is_dub))
                
        results = await asyncio.gather(*tasks)
        
        # Flatten the list of lists
        all_offers = []
        for offer_list in results:
            if offer_list:
                all_offers.extend(offer_list)
                
        return all_offers

    async def _safe_get_offers(self, provider: BaseProvider, mapped_id: str, episode: int, is_dub: bool):
        try:
            return await provider.get_source_offers(mapped_id, episode, is_dub)
        except Exception as e:
            print(f"[Provider Error] {provider.name} failed to get offers: {e}")
            return []

    def get_provider(self, name: str) -> BaseProvider:
        return self.providers.get(name.lower())

# Singleton instance to be used across the app
provider_manager = ProviderManager()