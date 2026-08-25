import unittest
from fastapi.testclient import TestClient

from app.main import app
from app.services import player_proxy as pp


class PlayerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_token_registration_and_retrieval(self):
        test_url = "https://cdn.example.com/hls/live/stream.m3u8"
        token = pp.register_stream(test_url)
        self.assertTrue(bool(token))
        resolved_url = pp._url_for_token(token)
        self.assertEqual(resolved_url, test_url)

    def test_token_retrieval_survives_instance_local_cache_loss(self):
        test_url = "https://cdn.example.com/hls/live/segment.m4s?token=abc"
        token = pp.register_stream(test_url)
        pp.url_tokens.clear()
        pp.reverse_tokens.clear()
        self.assertEqual(pp._url_for_token(token), test_url)

    def test_m3u8_rewriting(self):
        base_url = "https://cdn.example.com/hls/live/index.m3u8"
        token = pp.register_stream(base_url)
        sample_playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
segment0.ts
#EXT-X-KEY:METHOD=AES-128,URI="enc.key"
#EXTINF:10.0,
segment1.ts
#EXT-X-ENDLIST"""
        rewritten = pp._rewrite_m3u8(base_url, token, sample_playlist)
        self.assertIn(f"/hls/{token}/seg/", rewritten)
        self.assertIn(f"/hls/{token}/key/", rewritten)
        self.assertIn(".ts", rewritten)

    def test_route_register_stream(self):
        resp = self.client.get("/register", params={"url": "https://example.com/video/master.m3u8"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)
        self.assertIn("playlist", data)
        self.assertTrue(data["playlist"].startswith("/hls/"))

    def test_route_party_new(self):
        resp = self.client.get("/api/party/new")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("room", data)
        self.assertTrue(len(data["room"]) >= 5)

    def test_route_status(self):
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("tunnel_connected", data)

    def test_route_player_browse(self):
        resp = self.client.get("/player/browse")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_route_player_live(self):
        resp = self.client.get("/player/live", params={"stream_url": "https://example.com/live.m3u8"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("https://example.com/live.m3u8", resp.text)

    def test_route_player_stream_tv_movie(self):
        resp = self.client.get(
            "/player/stream/1368337",
            params={
                "title": "The Odyssey",
                "year": 2026,
                "seasonId": 1,
                "episodeId": 1,
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("The Odyssey", resp.text)

    def test_route_player_stream_anime_provider(self):
        resp = self.client.get(
            "/player/stream",
            params={
                "provider": "anidb",
                "ep_id": "1429_sub",
                "title": "Attack on Titan",
                "episodeId": 76,
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Attack on Titan", resp.text)

    def test_route_player_stream_direct_hls(self):
        resp = self.client.get(
            "/player/stream",
            params={
                "stream_url": "https://example.com/anime_stream.m3u8",
                "title": "Demon Slayer",
            }
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Demon Slayer", resp.text)

    def test_route_stream_metadata_lookup(self):
        test_url = "https://cdn.example.com/hls/channel.m3u8"
        token = pp.register_stream(test_url)
        resp = self.client.get("/api/stream/metadata", params={"token": token})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("title", data)
        self.assertIn("synopsis", data)


if __name__ == "__main__":
    unittest.main()
