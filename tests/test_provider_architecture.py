import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.player import MediaContext, SourceType
from app.models.media import MediaType
from app.providers.base import BaseProvider, encode_ep_id, decode_ep_id
from app.providers.manager import ProviderManager, _matches_type
from app.providers.anime.anidb import AniDBProvider
from app.providers.movies_tv.player import VideasyProvider


class ProviderArchitectureTests(unittest.IsolatedAsyncioTestCase):

    def test_media_type_matching(self):
        """1. Verify _matches_type correctly maps provider types to requested types."""
        self.assertTrue(_matches_type("movie_tv", "movie"))
        self.assertTrue(_matches_type("movie_tv", "tv"))
        self.assertTrue(_matches_type("movie_tv", "movie_tv"))
        self.assertTrue(_matches_type("movie_tv", "all"))
        self.assertTrue(_matches_type("anime", "anime"))
        self.assertTrue(_matches_type("anime", "all"))

        self.assertTrue(_matches_type("movie_tv", "anime"))
        self.assertFalse(_matches_type("anime", "movie"))
        self.assertFalse(_matches_type("anime", "tv"))

    def test_ep_id_encoding_decoding(self):
        """5. Verify encode_ep_id and decode_ep_id roundtrip & legacy fallback."""
        payload = {"ep_id": "12345", "is_dub": True, "provider": "anidb"}
        encoded = encode_ep_id(payload)
        self.assertIsInstance(encoded, str)

        decoded = decode_ep_id(encoded)
        self.assertEqual(decoded.get("ep_id"), "12345")
        self.assertTrue(decoded.get("is_dub"))

        # Legacy fallback
        legacy_str = "12345_dub"
        legacy_decoded = decode_ep_id(legacy_str)
        self.assertEqual(legacy_decoded.get("raw"), "12345_dub")

    async def test_connection_pooling(self):
        """6. Verify connection pool management across BaseProvider instances."""
        provider = BaseProvider()
        client1 = provider.client
        client2 = provider.client
        self.assertIs(client1, client2)
        await provider.close()
        self.assertTrue(client1.is_closed)

    async def test_anidb_caching_layer(self):
        """3 & 4. Verify ID and Episode Caching prevents redundant outbound calls in AniDBProvider."""
        mock_client = AsyncMock()
        
        # Mock search response
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.text = '<a href="https://anidb.app/anime/attack-on-titan-999" class="anime-card" title="Attack on Titan">'
        
        # Mock episode response
        ep_resp = MagicMock()
        ep_resp.status_code = 200
        ep_resp.json.return_value = {"episodes": [{"id": "ep101"}]}

        mock_client.get = AsyncMock(side_effect=[search_resp, ep_resp])

        provider = AniDBProvider(client=mock_client)
        context = MediaContext(
            mapped_id="a16498",
            primary_title="Attack on Titan",
            english_title="Attack on Titan"
        )

        # Call 1: Populates mapping & episode cache
        offers1 = await provider.get_source_offers("a16498", episode_absolute=1, is_dub=False, context=context)
        self.assertEqual(len(offers1), 1)
        self.assertEqual(mock_client.get.call_count, 2)

        # Call 2 (Episode 1 again): Should hit cache completely! zero HTTP calls!
        offers2 = await provider.get_source_offers("a16498", episode_absolute=1, is_dub=True, context=context)
        self.assertEqual(len(offers2), 1)
        self.assertEqual(mock_client.get.call_count, 2)  # Count remains 2!
        await provider.close()


if __name__ == "__main__":
    unittest.main()
