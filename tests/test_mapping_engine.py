import unittest
from app.core.database import MappingSessionLocal
from app.services.mapping_engine import MappingEngine


class MappingEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MappingSessionLocal()
        cls.mapper = MappingEngine(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_extract_id(self):
        self.assertEqual(self.mapper.extract_id("tt1429"), 1429)
        self.assertEqual(self.mapper.extract_id("tm550"), 550)
        self.assertEqual(self.mapper.extract_id("m16498"), 16498)
        self.assertEqual(self.mapper.extract_id("a16498"), 16498)
        self.assertEqual(self.mapper.extract_id("mal_52991"), 52991)

    def test_extract_prefix(self):
        self.assertEqual(self.mapper.extract_prefix("tt1429"), "tt")
        self.assertEqual(self.mapper.extract_prefix("tm550"), "tm")
        self.assertEqual(self.mapper.extract_prefix("m16498"), "m")
        self.assertEqual(self.mapper.extract_prefix("a16498"), "a")
        self.assertEqual(self.mapper.extract_prefix("mal_52991"), "mal")
        self.assertEqual(self.mapper.extract_prefix("anilist_16498"), "anilist")

    def test_get_all_ids_mal(self):
        res = self.mapper.get_all_ids("m16498")
        self.assertIsNotNone(res)
        self.assertEqual(res["mal_id"], 16498)
        self.assertEqual(res["tmdb_tv_id"], 1429)

    def test_get_all_ids_anilist(self):
        res = self.mapper.get_all_ids("a16498")
        self.assertIsNotNone(res)
        self.assertEqual(res["anilist_id"], 16498)
        self.assertEqual(res["mal_id"], 16498)

    def test_get_mal_id_for_tmdb_season(self):
        mal_id = self.mapper.get_mal_id_for_tmdb_season(1429, 1)
        self.assertEqual(mal_id, 16498)

    def test_batch_anime_detection(self):
        anime_ids = self.mapper.get_anime_tmdb_tv_ids([1429, 999999999])
        self.assertIn(1429, anime_ids)
        self.assertNotIn(999999999, anime_ids)


if __name__ == "__main__":
    unittest.main()
