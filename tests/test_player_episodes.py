"""Test suite for Animax Player Episode & Season Switcher endpoint."""

import unittest
from fastapi.testclient import TestClient
from app.main import app


class PlayerEpisodesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_tv_episodes_endpoint(self):
        """Verify /api/player/episodes/{media_id} returns seasons and episodes for TV show (tt1429)."""
        response = self.client.get("/api/player/episodes/tt1429")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["media_id"], "tt1429")
        self.assertIn("seasons", data)
        self.assertIn("episodes", data)
        self.assertGreater(len(data["seasons"]), 0)
        self.assertGreater(len(data["episodes"]), 0)

        first_ep = data["episodes"][0]
        self.assertIn("season_number", first_ep)
        self.assertIn("episode_number", first_ep)
        self.assertIn("absolute_number", first_ep)

    def test_anime_episodes_endpoint(self):
        """Verify /api/player/episodes/{media_id} returns episodes for Anime (m52991)."""
        response = self.client.get("/api/player/episodes/m52991")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["media_id"], "m52991")
        self.assertIn("episodes", data)
        self.assertGreater(len(data["episodes"]), 0)

    def test_invalid_media_id_graceful_response(self):
        """Verify /api/player/episodes/{media_id} handles non-existent IDs gracefully."""
        response = self.client.get("/api/player/episodes/invalid_9999999")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["media_id"], "invalid_9999999")
        self.assertEqual(data["episodes"], [])


if __name__ == "__main__":
    unittest.main()
