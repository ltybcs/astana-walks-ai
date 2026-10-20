"""One validated intent across source adapters; all sources are synthetic."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from app import dgis_live
from app.google_grounded import prepare, safe_intent
from app.retrieval import Retriever, detect_intent, source_goal_evidence
from app.semantic_intent import validate_intent
from app.unified_dgis import UnifiedDGIS
from scripts.collect_2gis import normalize_items, opening_hours
from tests.test_collection_2gis import item

ORIGIN = {"lat": 51.1283, "lon": 71.4305, "label": "PRIVATE_START_LABEL"}
STAMP = "2026-10-21T00:00:00+00:00"


def plan(query="Хочу в планетарий", *, goal="планетарий", search=None, category=None,
         fulfilment="venue_visit", requirements=None):
    return validate_intent({"start_quote": None, "goals": [{"quote": goal,
        "search_text": search or goal, "category_hint": category, "fulfilment": fulfilment}],
        "excluded": [], "goal_policy": "primary", "requirements": requirements or [], "context": []}, query)


def poi(identifier, category, **extra):
    return {"id": identifier, "name": "Синтетическое " + identifier, "category": category,
            "description": "Синтетический публичный источник.", "lat": ORIGIN["lat"] + .001,
            "lon": ORIGIN["lon"] + .001, "source_url": "https://www.openstreetmap.org/node/1",
            "opening_hours": None, "features": {}, **extra}


class SemanticSourceTests(unittest.TestCase):
    def test_one_plan_bypasses_lexical_rejection_without_generic_replacement(self):
        validated = plan()
        source = poi("osm:1", "planetarium", source_rubrics=["Планетарии"])
        unrelated = poi("osm:2", "cafe", name="Планетарий кафе")
        r = Retriever(data={"places": [source, unrelated]})
        with patch("app.retrieval.parse_search_intent", side_effect=AssertionError("Do not reparse")):
            value = r.retrieve("PRIVATE_RAW_MESSAGE near PRIVATE_HOME", ORIGIN, 120, intent_plan=validated)
            flags = detect_intent("PRIVATE_RAW_MESSAGE", intent_plan=validated)
        self.assertEqual([x["poi"]["id"] for x in value["selected"]], [source["id"]])
        self.assertTrue(flags["open_goal"])
        self.assertEqual(flags["preferred_categories"], [])
        self.assertEqual(flags["activity_mode"], "single_venue")
        self.assertNotIn("PRIVATE", json.dumps(flags))
        empty = Retriever(data={"places": [unrelated]}).retrieve("near", ORIGIN, 120, intent_plan=validated)
        self.assertEqual(empty["selected"], [])
        self.assertEqual(empty["filtered"]["no_verified_match"], 1)

    def test_unmapped_rubrics_are_ram_opt_in_not_cli_categories(self):
        raw = item(name="Синтетический планетарий", point={"lat": ORIGIN["lat"], "lon": ORIGIN["lon"]},
                   rubrics=[{"name": "Планетарии"}])
        original = deepcopy(raw)
        self.assertEqual(normalize_items([raw], STAMP), [])
        records = normalize_items([raw], STAMP, allow_unmapped=True)
        self.assertEqual(records[0]["category"], "unknown")
        self.assertEqual(records[0]["source_categories"], [])
        self.assertEqual(records[0]["source_rubrics"], ["Планетарии"])
        self.assertTrue(source_goal_evidence(records[0], plan()))
        self.assertEqual(raw, original)

    def test_type_identity_not_brand_description_or_related_venue(self):
        validated = plan()
        for record in (poi("x", "cafe", name="Планетарий", description="Планетарии"),
                       poi("x", "unknown", source_rubrics=["Обсерватории"]),
                       poi("x", "unknown", source_rubrics=["Детские планетарии"]),
                       poi("x", "unknown", source_type="branch")):
            with self.subTest(record=record):
                self.assertFalse(source_goal_evidence(record, validated))
        self.assertTrue(source_goal_evidence(poi("x", "unknown", source_rubrics=["ПЛАНЕТАРИИ"]), validated))

    def test_specific_activity_and_unknown_required_properties_never_follow_from_type(self):
        source = poi("z", "unknown", source_rubrics=["Зоопарки"])
        interaction = plan("Хочу погладить капибару", goal="погладить капибару", fulfilment="specific_activity")
        self.assertFalse(source_goal_evidence(source, interaction))
        for key, witness in (("equipment", "оборудование"), ("inventory", "товар в наличии"), ("price", "цена")):
            p = plan("Хочу в планетарий, обязательно " + witness,
                     requirements=[{"quote": witness, "key": key, "required": True}])
            self.assertFalse(source_goal_evidence(poi("x", "unknown", source_rubrics=["Планетарии"]), p))
        socket = plan("Хочу кафе с розеткой", goal="кафе", category="cafe", requirements=[{"quote": "розеткой", "key": "socket", "required": True}])
        self.assertFalse(source_goal_evidence(poi("c", "cafe"), socket))
        self.assertTrue(source_goal_evidence(poi("c", "cafe", features={"socket": "yes"}), socket))

    def test_google_optional_plan_uses_bounded_phrase_and_full_outbound_binding(self):
        validated = plan()
        context = {"origin": ORIGIN.copy(), "origin_id": "baiterek", "minutes": 120}
        raw = "PRIVATE_RAW_MESSAGE PRIVATE_HOME"
        with patch("app.google_grounded.parse_search_intent", side_effect=AssertionError("Do not reparse")):
            safe = safe_intent(raw, intent_plan=validated)
            got, intent, text, digest, params = prepare({"query": raw}, lambda _: {"query": raw, "resolved_context": context}, intent_plan=validated)
        self.assertEqual(safe["categories"], [])
        self.assertTrue(safe["open_goal"])
        self.assertIn("планетарий", text)
        self.assertNotIn("PRIVATE", params["textQuery"])
        self.assertEqual(got, context)
        self.assertEqual(len(digest), 64)
        self.assertEqual(params["pageSize"], 5)
        self.assertFalse(params["includeExtendedDetails"])

    def test_dgis_shared_single_attempt_queries_goal_but_does_not_infer_type(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DGIS_API_KEY": "SYNTHETIC_NOT_A_REAL_KEY"}):
            root = Path(directory).resolve()
            now = 1790000000.0
            legacy = dgis_live._LiveClient(root, lambda: now)
            legacy.status()
            records = [item(id="1", point={"lat": ORIGIN["lat"], "lon": ORIGIN["lon"]}, rubrics=[{"name": "Планетарии"}]),
                       item(id="2", name="Планетарий", point={"lat": ORIGIN["lat"], "lon": ORIGIN["lon"]}, rubrics=[{"name": "Кафе"}]),
                       item(id="3", point={"lat": ORIGIN["lat"], "lon": ORIGIN["lon"]}, rubrics=[{"name": "Обсерватории"}])]
            class Response(io.BytesIO):
                headers = {"Content-Type": "application/json"}
            transport = Mock(side_effect=lambda *a, **k: Response(json.dumps({"meta": {"code": 200}, "result": {"items": records}}).encode()))
            client = UnifiedDGIS(root, clock=lambda: now, http_open=transport)
            result = client.search("PRIVATE_RAW_MESSAGE", ORIGIN, 1000, intent_plan=plan())
            self.assertEqual([x["id"] for x in result["places"]], ["2gis:1"])
            self.assertEqual(result["places"][0]["source_rubrics"], ["Планетарии"])
            wire = parse_qs(urlsplit(transport.call_args.args[0].full_url).query)
            self.assertEqual(wire["q"], ["планетарий"])
            self.assertNotIn("PRIVATE", wire["q"][0])
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(result["quota"]["used"], 1)
            self.assertEqual(len(json.loads((legacy.directory / "usage.json").read_bytes())["attempts"]), 1)
            self.assertFalse(any(p.name.endswith(".json") and p.name != "usage.json" for p in legacy.directory.iterdir()))

    def test_generic_nature_plan_keeps_outdoor_candidates_not_cafe(self):
        validated = plan("Хочу на природу", goal="природу", search="прогулка на природе",
                         fulfilment="generic_walk")
        natural = poi("osm:park", "park")
        cafe = poi("osm:cafe", "cafe")
        value = Retriever(data={"places": [cafe, natural]}).retrieve("PRIVATE_RAW", ORIGIN, 90, intent_plan=validated)
        self.assertEqual([item["poi"]["id"] for item in value["selected"]], [natural["id"]])

    def test_schedule_requires_full_unconditional_week_and_missing_is_unknown(self):
        week = {day: {"working_hours": [{"from": "00:00", "to": "24:00"}]} for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
        self.assertEqual(opening_hours({"schedule": week}), "24/7")
        self.assertIsNone(opening_hours({}))
        self.assertIsNone(opening_hours({"schedule": {"is_24x7": True}}))
        for change in ({"date_from": "2026-12-20"}, {"date_to": "2026-01-01"},
                       {"comment": "Только в праздники"}, {"is_24x7": False}):
            self.assertNotEqual(opening_hours({"schedule": {**week, **change}}), "24/7")
        self.assertNotEqual(opening_hours({"schedule": week, "schedule_special": [{"date": "2026-12-31"}]}), "24/7")
