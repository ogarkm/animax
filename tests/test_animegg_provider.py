import unittest
from unittest.mock import AsyncMock, patch
import urllib.parse

from app.models.player import MediaContext, SourceType
from app.providers.base import encode_ep_id, decode_ep_id
from app.providers.anime.animegg import AnimeGGProvider, normalize_title, levenshtein_similarity


class AnimeGGProviderTests(unittest.IsolatedAsyncioTestCase):

    def test_normalize_title_and_similarity(self):
        self.assertEqual(normalize_title("Attack on Titan Season 2"), "attackontitan2")
        self.assertEqual(normalize_title("Naruto Shippuuden (Dub)"), "narutoshippuudendub")
        self.assertGreater(levenshtein_similarity("narutoshippuuden", "narutoshippuuden"), 0.99)
        self.assertLess(levenshtein_similarity("naruto", "onepiece"), 0.5)

    @patch.object(AnimeGGProvider, "_fetch_html")
    async def test_get_source_offers_success(self, mock_fetch):
        # 1. Mock search HTML
        search_html = """
        <!DOCTYPE html>
        <html>
        <body>
            <a href="/series/shingeki-no-kyojin" class="mse">
                <div class="thumb"><img src="/thumb.jpg" /></div>
                <h2>Attack on Titan</h2>
            </a>
        </body>
        </html>
        """

        # 2. Mock series HTML
        series_html = """
        <!DOCTYPE html>
        <html>
        <body>
            <a href="/shingeki-no-kyojin-episode-1" class="anm_det_pop">
                <strong>Episode 1</strong>
                <i class="anititle">To You, in 2000 Years</i>
            </a>
            <a href="/shingeki-no-kyojin-episode-2" class="anm_det_pop">
                <strong>Episode 2</strong>
                <i class="anititle">That Day</i>
            </a>
        </body>
        </html>
        """

        mock_fetch.side_effect = [search_html, series_html]

        provider = AnimeGGProvider()
        context = MediaContext(
            mapped_id="a16498",
            primary_title="Attack on Titan",
            english_title="Attack on Titan"
        )

        offers = await provider.get_source_offers(
            mapped_id="a16498",
            episode_absolute=1,
            is_dub=False,
            context=context
        )

        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer.provider, "animegg")
        self.assertEqual(offer.type, SourceType.INTERNAL)
        self.assertFalse(offer.dub)
        self.assertIn("/api/player/payload?provider=animegg&ep_id=", offer.url)
        self.assertIn("/player/stream?provider=animegg", offer.external_player_url)

        # Verify caching: second call for ep 2 should only fetch nothing if episodes cached
        offers_ep2 = await provider.get_source_offers(
            mapped_id="a16498",
            episode_absolute=2,
            is_dub=False,
            context=context
        )
        self.assertEqual(len(offers_ep2), 1)
        self.assertEqual(mock_fetch.call_count, 2)  # Search and series HTML were cached

    @patch.object(AnimeGGProvider, "_fetch_html")
    async def test_extract_stream_success(self, mock_fetch):
        # 1. Mock Episode Page HTML
        ep_html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="subbed-Animegg" class="tab-pane active">
                <iframe src="/embed/12345" width="100%" height="400"></iframe>
            </div>
            <div id="dubbed-Animegg" class="tab-pane">
                <iframe src="/embed/67890" width="100%" height="400"></iframe>
            </div>
        </body>
        </html>
        """

        # 2. Mock Embed Page HTML
        embed_html = """
        <!DOCTYPE html>
        <html>
        <body>
            <script>
                var videoSources = [
                    {file: "/videos/stream_720p.mp4", label: "720p"},
                    {file: "/videos/stream_1080p.mp4", label: "1080p"},
                    {file: "/videos/stream_480p.mp4", label: "480p"}
                ];
            </script>
        </body>
        </html>
        """

        mock_fetch.side_effect = [ep_html, embed_html]

        provider = AnimeGGProvider()
        payload_data = {
            "ep_href": "/shingeki-no-kyojin-episode-1",
            "is_dub": False,
            "media_title": "Attack on Titan",
            "episode_num": 1,
            "ep_title": "To You, in 2000 Years"
        }
        encoded_token = encode_ep_id(payload_data)

        state = await provider.extract_stream(encoded_token)

        self.assertEqual(state.provider_used, "animegg")
        self.assertEqual(state.stream_type, "mp4")
        self.assertEqual(state.media_title, "Attack on Titan")
        self.assertEqual(state.episode_title, "To You, in 2000 Years")
        # Should pick the highest quality (1080p)
        self.assertIn("stream_1080p.mp4", urllib.parse.unquote(state.stream_url))
        self.assertEqual(state.headers.get("Referer"), "https://www.animegg.org")


if __name__ == "__main__":
    unittest.main()
