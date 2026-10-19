"""Regressions observed by Scirtor on the retained OSM snapshot."""

import unittest

from app.evaluate import evaluate
from app.generation import recommend
from app.retrieval import Retriever, architecture_evidence


class RealDatasetQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retriever = Retriever()

    def test_default_is_retained_real_snapshot(self):
        self.assertEqual(self.retriever.data_path.name, "poi.astana.json")
        self.assertFalse(self.retriever.dataset["is_demo"])
        self.assertGreater(len(self.retriever.places), 500)

    def test_two_mega_osm_sources_are_one_physical_stop(self):
        duplicate = "osm:node/4965336822"
        canonical = "osm:way/499328514"
        self.assertEqual(self.retriever.duplicate_of.get(duplicate), canonical)
        response = recommend(self.retriever, "Шопинг возле MEGA Silk Way", 60, {"lat": 51.089347, "lon": 71.405441}, use_llm=False)
        ids = {item["id"] for item in response["recommendations"]}
        self.assertIn(canonical, ids)
        self.assertNotIn(duplicate, ids)
        chosen = next(item for item in response["recommendations"] if item["id"] == canonical)
        self.assertIn("10:00-22:00", chosen["opening_note"])

    def test_architecture_query_keeps_nearby_landmark_and_excludes_unsupported_rides(self):
        response = recommend(self.retriever, "Я турист, хочу посмотреть архитектуру", 120, {"lat": 51.1282882, "lon": 71.4304546}, use_llm=False)
        ids = {item["id"] for item in response["recommendations"]}
        self.assertIn("osm:way/230401645", ids)  # Байтерек at the chosen start
        self.assertNotIn("osm:way/1449322337", ids)  # НЛО, attraction only
        self.assertNotIn("osm:way/1449322339", ids)  # Динопарк, attraction only
        source = {item["id"]: item for item in self.retriever.places}
        self.assertTrue(all(architecture_evidence(source[identifier]) for identifier in ids))

    def test_real_five_scenarios_have_grounded_results_or_honest_abstention(self):
        report = evaluate(self.retriever, use_llm=False)
        self.assertEqual(report["summary"]["scenarios_passed"], 5)
        self.assertFalse(report["dataset"]["is_demo"])
        self.assertFalse(report["live_llm_used"])
        self.assertEqual(report["scenarios"][1]["result_count"], 0)
        self.assertIsNotNone(report["scenarios"][1]["clarification"])

    def test_radkurzior_family_park_request_does_not_include_bon_coffee(self):
        response = recommend(self.retriever, "С ребёнком, площадка или парк", 60, {"lat": 51.1546, "lon": 71.4252}, use_llm=False)
        self.assertTrue(response["recommendations"])
        self.assertTrue(all(item["category"] in {"park", "playground"} for item in response["recommendations"]))
        self.assertLessEqual(response["plan"]["total_minutes"], 60)
