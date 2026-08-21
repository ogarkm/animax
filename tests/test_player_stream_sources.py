import unittest

from player import main


class PlayerStreamSourceParsingTests(unittest.TestCase):
    def test_parse_stream_sources_from_json_list(self):
        raw = [
            {"id": "one", "title": "One", "url": "https://example.com/1.m3u8", "is_default": True},
            {"title": "Two", "stream_url": "https://example.com/2.m3u8"},
            "https://example.com/3.m3u8",
        ]

        parsed = main._parse_stream_sources_payload(raw)

        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["title"], "One")
        self.assertEqual(parsed[0]["url"], "https://example.com/1.m3u8")
        self.assertTrue(parsed[0]["is_default"])
        self.assertEqual(parsed[1]["title"], "Two")
        self.assertEqual(parsed[2]["title"], "Stream")

    def test_parse_stream_sources_from_json_string(self):
        parsed = main._parse_stream_sources_payload('[{"title":"Alpha","url":"https://example.com/a.m3u8"}]')

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["title"], "Alpha")
        self.assertEqual(parsed[0]["url"], "https://example.com/a.m3u8")

    def test_build_speedracelight_params_includes_seed_and_metadata(self):
        params = main._build_speedracelight_params(
            tmdb_id="1368337",
            media_type="movie",
            title="The Odyssey",
            year=2026,
            imdb_id="tt33764258",
            season=1,
            episode=1,
            seed="59477879.yN1fv3uze4OSR-OoBRhK86",
        )

        self.assertEqual(params["mediaType"], "movie")
        self.assertEqual(params["tmdbId"], "1368337")
        self.assertEqual(params["title"], "The Odyssey")
        self.assertEqual(params["year"], "2026")
        self.assertEqual(params["imdbId"], "tt33764258")
        self.assertEqual(params["enc"], "2")
        self.assertEqual(params["seed"], "59477879.yN1fv3uze4OSR-OoBRhK86")
        self.assertEqual(params["seasonId"], "1")
        self.assertEqual(params["episodeId"], "1")

    def test_extract_seed_from_payload(self):
        self.assertEqual(
            main._extract_seed_from_payload({"seed": "59477879.yN1fv3uze4OSR-OoBRhK86", "ttlMs": 30000}),
            "59477879.yN1fv3uze4OSR-OoBRhK86",
        )
        self.assertIsNone(main._extract_seed_from_payload({"ttlMs": 30000}))
        self.assertIsNone(main._extract_seed_from_payload(None))


if __name__ == "__main__":
    unittest.main()
