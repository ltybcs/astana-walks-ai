"""Offline category-to-route orchestration, using actual resolver/retriever.

Google/DGIS transports and narration are in-memory fakes. This proves local
control flow and conservative evidence checks, not live provider coverage.
"""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

from app.category_catalog import CATEGORY_CATALOG
from app.google_budget import GoogleError
from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.retrieval import Retriever
from app.unified_walks import UnifiedWalks
from tests.test_unified_walks import Google, poi


CLIENT = {"instant": "2026-10-20T06:00:00Z", "utc_offset_minutes": 300}
NOW = datetime(2026, 10, 20, 6, tzinfo=timezone.utc)


def resolve(payload):
    return resolve_request(payload, now=NOW)

VENUES = (
    ("playground", "детскую площадку"), ("cafe", "кафе"),
    ("restaurant", "ресторан"), ("bar", "бар"), ("museum", "музей"),
    ("theatre", "театр"), ("mall", "торговый центр"),
    ("computer_club", "компьютерный клуб"), ("cinema", "кинотеатр"),
    ("bowling", "боулинг"), ("billiards", "бильярд"), ("karaoke", "караоке"),
    ("escape_room", "квест"), ("board_games", "настольные игры"),
    ("anti_cafe", "антикафе"), ("trampoline", "батутный центр"),
    ("climbing", "скалодром"), ("ice_rink", "каток"), ("pool", "бассейн"),
    ("spa", "спа"), ("amusement_park", "парк аттракционов"),
    ("fitness", "фитнес"), ("bookshop", "книжный магазин"),
    ("library", "библиотеку"), ("coworking", "коворкинг"),
    ("nightclub", "ночной клуб"),
)


def fixture(origin, category, identifier="2gis:1", **overrides):
    value = poi(identifier, "Синтетическое место " + category, origin["lat"] + .002, origin["lon"] + .002, category)
    value.update(overrides)
    return value


def make_service(origin, places):
    google, dgis = Google(), Mock()
    unrelated = fixture(origin, "park", "osm:node/1")
    osm = Retriever(data={"dataset": {"is_demo": True}, "places": [unrelated]})
    dgis.status.return_value = {"configured": True, "available": True}
    dgis.search.return_value = {"places": deepcopy(places), "warnings": []}
    def narrate(grounding, stops, *_):
        return {"text": "Цельный синтетический тестовый ответ; не живые факты.",
                "source_ids": [stop["source_id"] for stop in stops],
                "stop_ids": [stop["stop_id"] for stop in stops]}, "mock_validated"
    narrator = Mock(side_effect=narrate)
    return UnifiedWalks(resolve, osm, google, dgis, narrator=narrator), google, dgis, narrator


class CategoryUnifiedTests(unittest.TestCase):
    def test_every_single_venue_type_builds_one_matching_venue_with_roundtrip_and_one_narration(self):
        expected = {identifier for identifier, spec in CATEGORY_CATALOG.items() if spec["mode"] == "single_venue"}
        self.assertEqual({identifier for identifier, _ in VENUES}, expected)
        origin = PUBLIC_ORIGINS["baiterek"]
        for category, phrase in VENUES:
            with self.subTest(category=category):
                target = fixture(origin, category, opening_hours="24/7")
                wrong = fixture(origin, "park", "2gis:2")
                service, google, dgis, narrator = make_service(origin, [wrong, target])
                result = service.recommend({"query": "Хочу " + phrase + " возле Байтерека на 120 минут",
                                            "client_context": CLIENT, "consent": True})
                option = result["alternatives"][0]
                self.assertEqual([stop["id"] for stop in option["stops"]], [target["id"]])
                self.assertEqual([stop["category"] for stop in option["stops"]], [category])
                self.assertEqual(option["budget_minutes"], 120)
                self.assertTrue(option["route_built"])
                self.assertEqual(option["budget_status"], "fits")
                plan = option["plan"]
                self.assertTrue(plan["verified"] and plan["returns_to_origin"])
                self.assertEqual(plan["total_minutes"], 120)
                self.assertEqual(plan["visit_minutes"], 120 - plan["walk_minutes"] - plan["buffer_minutes"])
                self.assertEqual(plan["legs"][0]["from_ref"], "origin")
                self.assertEqual(plan["legs"][-1]["to_ref"], "origin")
                self.assertEqual(len(google.legs), 2)
                self.assertEqual(narrator.call_count, 1)
                self.assertEqual(result["source_receipt"]["dgis"]["candidate_count"], 1)
                self.assertEqual(result["source_receipt"]["osm"]["candidate_count"], 0)
                self.assertEqual(result["source_receipt"]["google"]["candidate_count"], 0)
                self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")
                self.assertEqual(len(result["grounding"]["sources"]), 1)
                self.assertEqual(google.search_count, 1)
                self.assertEqual(dgis.search.call_count, 1)

    def test_absent_requested_type_does_not_route_a_park_or_generate_a_replacement(self):
        origin = PUBLIC_ORIGINS["baiterek"]
        for category, phrase in VENUES:
            with self.subTest(category=category):
                service, google, _, narrator = make_service(origin, [fixture(origin, "park")])
                result = service.recommend({"query": "Хочу " + phrase + " возле Байтерека на 120 минут",
                                            "client_context": CLIENT, "consent": True})
                self.assertEqual(google.legs, [])
                narrator.assert_not_called()
                self.assertIsNone(result["answer"])
                self.assertTrue(all(not option["route_built"] and not option["stops"] for option in result["alternatives"]))
                self.assertEqual(result["diagnostics"]["llm_attempts"], 0)
                self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")

    def test_bowling_then_coffee_cannot_be_satisfied_by_a_cafe_and_warns_about_secondary_goal(self):
        origin = PUBLIC_ORIGINS["baiterek"]
        cafe, bowling = fixture(origin, "cafe"), fixture(origin, "bowling", "2gis:2")
        for available, expected in (([cafe], []), ([cafe, bowling], [bowling["id"]])):
            with self.subTest(bowling=bool(expected)):
                service, google, _, narrator = make_service(origin, available)
                result = service.recommend({"query": "Хочу в боулинг и кофе возле Байтерека на 120 минут",
                                            "client_context": CLIENT, "consent": True})
                self.assertEqual([stop["id"] for stop in result["alternatives"][0]["stops"]], expected)
                self.assertEqual(narrator.call_count, int(bool(expected)))
                self.assertEqual(len(google.legs), 2 if expected else 0)
                self.assertTrue(any("Дополнительные типы мест отдельно не проверены" in warning for warning in result["warnings"]))

    def test_explicit_or_preserves_both_relevant_categories_without_origin_park(self):
        query = "Еду с ребёнком в центральный парк — где рядом есть детские площадки или музей с интерактивами?"
        resolved = resolve({"query": query, "client_context": CLIENT})
        origin = resolved["origin"]
        places = [fixture(origin, "playground"), fixture(origin, "museum", "2gis:2"),
                  fixture(origin, "park", "2gis:3")]
        service, _, _, narrator = make_service(origin, places)
        result = service.recommend({"query": query, "client_context": CLIENT, "consent": True})
        self.assertEqual(result["resolved_context"]["origin_id"], "central_park")
        self.assertEqual(result["resolved_context"]["search_intent"]["categories"], ["playground", "museum"])
        self.assertEqual(result["resolved_context"]["activity_mode"], "walk")
        self.assertEqual({stop["category"] for stop in result["alternatives"][0]["stops"]}, {"playground", "museum"})
        self.assertEqual(result["source_receipt"]["dgis"]["candidate_count"], 2)
        self.assertEqual(narrator.call_count, 1)

    def test_unknown_hard_properties_and_night_hours_are_not_inferred_from_the_request(self):
        origin = PUBLIC_ORIGINS["baiterek"]
        for extra in ("с розеткой", "для коляски", "только тихий", "ночью"):
            for confirmed in (False, True):
                with self.subTest(extra=extra, confirmed=confirmed):
                    features = {"socket": "yes", "wheelchair": "yes", "quiet": "yes"} if confirmed else {}
                    target = fixture(origin, "bowling", features=features,
                                     opening_hours="24/7" if confirmed else None)
                    service, google, _, narrator = make_service(origin, [target])
                    result = service.recommend({"query": "Хочу боулинг " + extra + " возле Байтерека на 120 минут",
                                                "client_context": CLIENT, "consent": True})
                    self.assertEqual(bool(result["alternatives"][0]["stops"]), confirmed)
                    self.assertEqual(narrator.call_count, int(confirmed))
                    self.assertEqual(len(google.legs), 2 if confirmed else 0)

    def test_six_user_examples_pass_the_actual_context_and_source_flow(self):
        cases = (
            ("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут — что посоветуете?", "mega", "park"),
            ("Хочу тихое место с кофе и розеткой недалеко от байтерека.", "baiterek", "cafe"),
            ("Еду с ребёнком в центральный парк — где рядом есть детские площадки или музей с интерактивами?", "central_park", "playground"),
            ("Я - турист около аэропорта, у меня 2 часа перед вылетом: что можно посмотреть поблизости?", "airport", "museum"),
            ("Ищу необычную ночную активность рядом с абу даби.", "abu_dhabi", "park"),
            ("Я у здании Экспо 2017, хочу что-то необычное на 1–2 часа, пешком.", "expo", "park"),
        )
        for query, origin_id, category in cases:
            with self.subTest(origin_id=origin_id):
                payload = {"query": query, "client_context": CLIENT}
                context = resolve(payload)
                self.assertEqual(context["resolved_context"]["origin_id"], origin_id)
                origin = context["origin"]
                target = fixture(origin, category, features={"socket": "yes", "quiet": "yes"},
                                 opening_hours="24/7")
                service, _, _, narrator = make_service(origin, [target])
                result = service.recommend({**payload, "consent": True})
                self.assertEqual([option["budget_minutes"] for option in result["alternatives"]],
                                 context["resolved_context"]["budget_options"])
                self.assertTrue(any(option["route_built"] for option in result["alternatives"]))
                self.assertEqual(narrator.call_count, 1)
                self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")
                for option in result["alternatives"]:
                    if option["route_built"]:
                        self.assertLessEqual(option["plan"]["total_minutes"], option["budget_minutes"])
                        self.assertEqual(option["plan"]["legs"][-1]["to_ref"], "origin")
                if origin_id == "airport":
                    self.assertTrue(any("аэропорт" in warning.lower() or "рейс" in warning.lower()
                                        for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
