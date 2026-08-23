import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.models.media import BaseMediaCard, MediaType
from app.providers.metadata import anilist
from app.routers import discovery


class AnimeCatalogTests(unittest.IsolatedAsyncioTestCase):
    def test_group_schedule_by_weekday_returns_stable_week(self):
        entries = [
            {"id": "a2", "release_date": "2026-08-24", "airing_at": 2},
            {"id": "a1", "release_date": "2026-08-23", "airing_at": 1},
        ]

        grouped = discovery.group_schedule_by_weekday(entries)

        self.assertEqual(list(grouped), [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ])
        self.assertEqual(grouped["sunday"][0]["id"], "a1")
        self.assertEqual(grouped["monday"][0]["id"], "a2")

    async def test_schedule_is_cached_after_provider_fetch(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        entries = [{"id": "a1", "release_date": "2026-08-24", "airing_at": 1}]

        with patch.object(discovery, "fetch_anilist_schedule", new=AsyncMock(return_value=entries)) as fetch:
            result = await discovery.get_release_schedule(days=7, db=db)

        fetch.assert_awaited_once_with(days=7)
        self.assertEqual(result["monday"][0]["id"], "a1")
        db.add.assert_called_once()
        db.commit.assert_called_once()

    async def test_anime_catalog_forwards_sort_and_page(self):
        card = BaseMediaCard(id="a1", title="Example", type=MediaType.ANIME)

        with patch.object(discovery, "fetch_anilist_list_safe", new=AsyncMock(return_value=[card])) as fetch:
            result = await discovery.get_anime_catalog(sort="score_desc", page=2)

        fetch.assert_awaited_once_with("ANIME", ["SCORE_DESC"], page=2)
        self.assertEqual(result["anime"][0]["id"], "a1")

    async def test_anime_catalog_rejects_unknown_sort(self):
        with self.assertRaises(HTTPException) as error:
            await discovery.get_anime_catalog(sort="random")

        self.assertEqual(error.exception.status_code, 400)

    async def test_fetch_anilist_schedule_falls_back_when_anilist_is_disabled(self):
        anime_response = MagicMock()
        anime_response.status_code = 200
        anime_response.json.return_value = {
            "data": [
                {
                    "id": "12",
                    "attributes": {
                        "canonicalTitle": "One Piece",
                        "titles": {"en": "One Piece"},
                        "status": "current",
                        "posterImage": {"large": "https://example.com/poster.jpg"},
                        "coverImage": {"large": "https://example.com/cover.jpg"},
                    },
                }
            ]
        }

        bad_response = MagicMock()
        bad_response.status_code = 403
        bad_response.json.return_value = {
            "errors": [{"message": "AniList API has been temporarily disabled due to severe stability issues."}]
        }

        with patch("app.providers.metadata.anilist.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.post = AsyncMock(return_value=bad_response)
            client.get = AsyncMock(return_value=anime_response)
            client_cls.return_value = client

            result = await anilist.fetch_anilist_schedule(days=7)

        self.assertTrue(result)
        self.assertEqual(result[0]["title"], "One Piece")
        self.assertIn("airing_at", result[0])


if __name__ == "__main__":
    unittest.main()