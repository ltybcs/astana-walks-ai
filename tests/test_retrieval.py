from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from app.generation import recommend
from app.retrieval import ROOT, Retriever, walk_minutes
from app.server import static_target, validate_request
from scripts.collect_osm import build_query, normalize_elements


ORIGIN = {"lat": 51.0892, "lon": 71.4074}


def poi(identifier, *, category="park", lat=51.09, lon=71.408, features=None, opening_hours=None, visit=15):
    return {
        "id": identifier, "name": identifier, "category": category,
        "description": "Наблюдение из тестового источника.",
        "lat": lat, "lon": lon, "features": features or {},
        "opening_hours": opening_hours, "visit_minutes": visit,
        "source_url": "https://www.openstreetmap.org/node/1",
    }


def index(*places):
    return Retriever(data={"dataset": {"is_demo": False, "collected_at": "2026-10-19"}, "places": list(places)})


class RetrievalTests(unittest.TestCase):
    def test_whole_itinerary_including_return_fits_budget(self):
        retriever = index(poi("a"), poi("b", lat=51.091), poi("c", lat=51.092), poi("d", lat=51.093))
        response = recommend(retriever, "парк прогулка", 45, ORIGIN, use_llm=False)
        plan = response["plan"]
        self.assertGreaterEqual(len(response["recommendations"]), 1)
        self.assertLessEqual(len(response["recommendations"]), 3)
        self.assertLessEqual(plan["total_minutes"], 45)
        self.assertEqual(plan["total_minutes"], plan["walk_minutes"] + plan["visit_minutes"])
        self.assertEqual(plan["walk_minutes"], sum(item["walk_minutes"] for item in response["recommendations"]) + plan["return_walk_minutes"])
        last = response["recommendations"][-1]
        self.assertEqual(plan["return_walk_minutes"], walk_minutes(last, ORIGIN))

    def test_far_away_candidate_is_excluded_even_if_exact_query_matches(self):
        retriever = index(poi("любимый парк", lat=51.20, lon=71.55), poi("near"))
        response = recommend(retriever, "любимый парк", 30, ORIGIN, use_llm=False)
        self.assertEqual([item["id"] for item in response["recommendations"]], ["near"])

    def test_unknown_socket_never_passes_a_socket_requirement(self):
        retriever = index(poi("wifi", category="cafe", features={"wifi": "yes"}), poi("socket", category="cafe", features={"socket": "yes"}))
        response = recommend(retriever, "тихий кофе с розеткой", 60, ORIGIN, use_llm=False)
        self.assertEqual([item["id"] for item in response["recommendations"]], ["socket"])
        self.assertTrue(any("Тишина" in warning for warning in response["warnings"]))

    def test_unknown_quietness_stays_unknown_and_action_is_a_concrete_idea(self):
        response = recommend(index(poi("cafe", category="cafe")), "Тихое кафе", 60, ORIGIN, use_llm=False)
        item = response["recommendations"][0]
        self.assertTrue(item["action"].startswith("Идея: Выпить кофе"))
        self.assertIn("если заведение открыто", item["action"])
        self.assertNotIn("тих", item["why"].lower())
        self.assertTrue(any("Уровень шума не подтверждён" in warning for warning in response["warnings"]))

    def test_missing_socket_evidence_produces_no_invented_recommendation(self):
        response = recommend(index(poi("cafe", category="cafe")), "Кофе и розетка", 60, ORIGIN, use_llm=False)
        self.assertEqual(response["recommendations"], [])
        self.assertIn("розеткой", response["clarification"])

    def test_night_requires_explicit_24_7_and_still_warns_not_live(self):
        response = recommend(index(poi("unknown"), poi("24h", opening_hours="24/7")), "Ночная прогулка", 60, ORIGIN, "night", use_llm=False)
        self.assertEqual([item["id"] for item in response["recommendations"]], ["24h"])
        self.assertIn("не проверен", response["recommendations"][0]["opening_note"])

    def test_child_context_excludes_bars(self):
        response = recommend(index(poi("bar", category="bar"), poi("park", category="park")), "Погулять с ребёнком", 90, ORIGIN, use_llm=False)
        self.assertEqual([item["id"] for item in response["recommendations"]], ["park"])
        self.assertTrue(any("Возрастные" in text for text in response["warnings"]))

    def test_wheelchair_unknown_is_not_positive(self):
        response = recommend(index(poi("unknown")), "Нужна доступность для коляски", 60, ORIGIN, use_llm=False)
        self.assertEqual(response["recommendations"], [])
        self.assertIn("доступностью", response["clarification"])

    def test_each_recommendation_has_matching_source_and_no_open_now_claim(self):
        response = recommend(index(poi("known")), "прогулка", 60, ORIGIN, use_llm=False)
        self.assertEqual({item["source_id"] for item in response["recommendations"]}, {source["id"] for source in response["sources"]})
        self.assertIn("не подтверждены", response["recommendations"][0]["opening_note"])
        self.assertEqual(response["mode"], "extractive")

    def test_sample_is_labeled_demo_and_budget_examples_work(self):
        retriever = Retriever(ROOT / "data" / "poi.sample.json")
        for minutes in (30, 60, 120):
            response = recommend(retriever, "прогулка возле Mega", minutes, ORIGIN, use_llm=False)
            self.assertTrue(response["recommendations"])
            self.assertLessEqual(response["plan"]["total_minutes"], minutes)
            self.assertEqual(response["mode"], "extractive/demo")
            self.assertTrue(all(item["confidence"] == "demo_unverified" for item in response["recommendations"]))

    @patch.dict("os.environ", {"LLM_API_KEY": "not-a-real-key", "LLM_MODEL": "test-model"})
    def test_llm_cannot_invent_a_source_or_facts(self):
        envelope = {"choices": [{"message": {"content": json.dumps({"recommendations": [{"id": "invented", "why": "Произвольное объяснение.", "action": "Идея: осмотреть место снаружи.", "citations": ["invented"], "fact_keys": ["category"]}]})}}]}
        with patch("app.llm.open_url", return_value=io.BytesIO(json.dumps(envelope).encode())):
            response = recommend(index(poi("known")), "прогулка", 60, ORIGIN)
        self.assertEqual(response["mode"], "extractive")
        self.assertEqual(response["recommendations"][0]["id"], "known")
        self.assertTrue(any("не прошёл проверку" in warning for warning in response["warnings"]))

    @patch.dict("os.environ", {"LLM_API_KEY": "not-a-real-key", "LLM_MODEL": "test-model"})
    def test_valid_llm_produces_free_grounded_explanations(self):
        envelope = {"choices": [{"message": {"content": json.dumps({"recommendations": [{"id": "known", "why": "Парк подходит для короткой прогулки на воздухе.", "action": "Идея: пройтись по парку после проверки доступа.", "citations": ["known"], "fact_keys": ["category"]}]})}}]}
        with patch("app.llm.open_url", return_value=io.BytesIO(json.dumps(envelope).encode())):
            response = recommend(index(poi("known")), "прогулка", 60, ORIGIN)
        self.assertEqual(response["mode"], "llm-grounded")
        self.assertTrue(response["recommendations"][0]["why"].startswith("Парк подходит"))


class ValidationTests(unittest.TestCase):
    def test_static_allowlist_blocks_traversal_and_repository_files(self):
        for path in ("/../.env", "/.env", "/app/server.py", "/%2e%2e/.env", "/data/poi.sample.json"):
            self.assertIsNone(static_target(path))
        self.assertEqual(static_target("/"), "/index.html")

    def test_rejects_invalid_coordinate_types_and_values(self):
        for value in (float("nan"), float("inf"), True, "51.1", 91):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_request({"query": "парк", "minutes": 60, "origin": {"lat": value, "lon": 71.4}})

    def test_rejects_invalid_budget_and_empty_question(self):
        for minutes in (True, 0, 241, 60.0, "60"):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                validate_request({"query": "парк", "minutes": minutes, "origin": ORIGIN})
        with self.assertRaises(ValueError):
            validate_request({"query": " ", "minutes": 60, "origin": ORIGIN})

    def test_rejects_unknown_time_context(self):
        for context in ("now", None, ["night"]):
            with self.subTest(context=context), self.assertRaises(ValueError):
                validate_request({"query": "парк", "minutes": 60, "origin": ORIGIN, "time_of_day": context})


class CollectionTests(unittest.TestCase):
    def test_normalizes_center_sources_unknowns_and_preserves_evidence(self):
        payload = {"elements": [{"type": "way", "id": 7, "center": ORIGIN, "timestamp": "2026-10-01T00:00:00Z", "tags": {"name": "Cafe", "amenity": "cafe", "internet_access": "wlan"}}]}
        places = normalize_elements(payload, "2026-10-19T00:00:00Z")
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["features"]["socket"], "unknown")
        self.assertEqual(places[0]["features"]["quiet"], "unknown")
        self.assertEqual(places[0]["source_url"], "https://www.openstreetmap.org/way/7")
        self.assertEqual(places[0]["source_updated_at"], "2026-10-01T00:00:00Z")
        self.assertIsNone(places[0]["opening_hours"])

    def test_explicit_socket_tag_is_evidence_and_duplicates_are_removed(self):
        element = {"type": "node", "id": 8, **ORIGIN, "tags": {"name": "Cafe", "amenity": "cafe", "socket:schuko": "2", "opening_hours": "24/7", "wheelchair": "yes"}}
        places = normalize_elements({"elements": [element, element]}, "2026-10-19")
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["features"]["socket"], "yes")
        self.assertEqual(places[0]["features"]["wheelchair"], "yes")
        self.assertEqual(places[0]["source_tags"]["socket:schuko"], "2")

    def test_invalid_or_unnamed_geometries_are_ignored(self):
        payload = {"elements": [
            {"type": "node", "id": 1, **ORIGIN, "tags": {"amenity": "cafe"}},
            {"type": "node", "id": 2, "lat": float("nan"), "lon": 71, "tags": {"name": "bad", "amenity": "cafe"}},
        ]}
        self.assertEqual(normalize_elements(payload, "2026-10-19"), [])

    def test_query_collects_nodes_ways_relations_and_provenance(self):
        query = build_query((51.02, 71.30, 51.22, 71.55))
        self.assertIn('nwr["amenity"="cafe"]', query)
        self.assertIn("out center tags meta", query)


if __name__ == "__main__":
    unittest.main()
