"""Quality gates for the checked-in real OSM snapshot and its normalization."""

import json
from pathlib import Path
import unittest

from app.generation import recommend
from app.retrieval import Retriever
from scripts.collect_osm import DEFAULT_BBOX, build_query, normalize_elements


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/poi.astana.json"


class RealAstanaDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(DATA.read_text(encoding="utf-8"))
        cls.metadata = cls.document["dataset"]
        cls.places = cls.document["places"]

    def test_real_sources_coordinates_and_snapshot_provenance(self):
        self.assertFalse(self.metadata["is_demo"])
        self.assertEqual(self.metadata["license"], "ODbL 1.0")
        self.assertGreater(len(self.places), 100)
        self.assertEqual(len(self.places), len({p["id"] for p in self.places}))
        south, west, north, east = self.metadata["bbox"]
        for place in self.places:
            with self.subTest(id=place["id"]):
                self.assertTrue(place["name"].strip())
                self.assertTrue(south <= place["lat"] <= north)
                self.assertTrue(west <= place["lon"] <= east)
                self.assertEqual(place["source_url"], "https://www.openstreetmap.org/" + place["id"].removeprefix("osm:"))
                self.assertEqual(place["verification"], "source_reported_not_field_verified")
                self.assertIsNotNone(place["source_updated_at"])

    def test_normalized_data_is_reproducible_from_retained_raw_snapshot(self):
        raw = json.loads((ROOT / self.metadata["raw_snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(raw["osm3s"]["timestamp_osm_base"], self.metadata["source_snapshot_at"])
        profile = self.metadata.get("query_profile", "legacy_named")
        self.assertEqual(normalize_elements(raw, self.metadata["collected_at"], bbox=self.metadata["bbox"], profile=profile), self.places)
        self.assertEqual((ROOT / self.metadata["query_file"]).read_text(encoding="utf-8"), build_query(tuple(self.metadata["bbox"]), profile=profile))

    def test_landmarks_and_unknown_attributes_are_retained_without_fabrication(self):
        by_id = {p["id"]: p for p in self.places}
        self.assertIn("osm:way/499328514", by_id)  # MEGA
        self.assertIn("osm:way/230401645", by_id)  # Bayterek
        self.assertIn("osm:way/1239177393", by_id)  # Atyrau bridge
        for place in self.places:
            self.assertIn(place["features"]["socket"], {"yes", "no", "unknown"})
            self.assertEqual(place["features"]["quiet"], "unknown")
            if "opening_hours" not in place["source_tags"]:
                self.assertIsNone(place["opening_hours"])

    def test_five_synthetic_scenarios_fit_budget_and_keep_hard_constraints(self):
        retriever = Retriever(DATA)
        mega = {"lat": 51.089347, "lon": 71.405441}
        cases = [("прогулка возле MEGA Silk Way", 60, mega, "day"),
                 ("тихое кафе с розеткой", 60, mega, "day"),
                 ("прогуляться с ребёнком", 90, mega, "day"),
                 ("турист, посмотреть архитектуру", 120, {"lat": 51.1282882, "lon": 71.4304546}, "day"),
                 ("ночная прогулка", 60, {"lat": 51.1560749, "lon": 71.4249916}, "night")]
        by_id = {p["id"]: p for p in self.places}
        for query, minutes, origin, context in cases:
            with self.subTest(query=query):
                result = recommend(retriever, query, minutes, origin, context, use_llm=False)
                self.assertEqual(result["mode"], "extractive")
                self.assertLessEqual(result["plan"]["total_minutes"], minutes)
                self.assertLessEqual(len(result["recommendations"]), 3)
                for place in result["recommendations"]:
                    self.assertIn(place["id"], by_id)
                    if "розеткой" in query:
                        self.assertEqual(by_id[place["id"]]["features"]["socket"], "yes")
                    if "ребёнком" in query:
                        self.assertNotEqual(place["category"], "bar")
                    if context == "night":
                        self.assertEqual(by_id[place["id"]]["opening_hours"], "24/7")
                if not result["recommendations"]:
                    self.assertTrue(result.get("clarification"))


if __name__ == "__main__":
    unittest.main()
