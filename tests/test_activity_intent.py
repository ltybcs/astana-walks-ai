"""Activity relevance regressions; all provider calls use in-memory fakes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from app.activity_intent import activity_intent
from app.google_budget import GoogleError
from app.google_grounded import safe_intent, prepare
from app.lab_context import resolve_request
from app.retrieval import Retriever, detect_intent
from app.unified_walks import UnifiedWalks
from tests.test_unified_walks import Google, poi


class ActivityIntentTests(unittest.TestCase):
    def test_gaming_aliases_preserve_one_canonical_activity_across_searches(self):
        for phrase in ("комп клуб", "компьютерный клуб", "компьютерном клубе", "киберклуб", "интернет-клуб", "gaming club", "computer club", "PC gaming lounge", "esports club", "ПК-клуб", "PC-клуб"):
            query = "Хочу " + phrase + " рядом с Байтереком"
            with self.subTest(phrase=phrase):
                self.assertEqual(detect_intent(query)["preferred_categories"], ["computer_club"])
                self.assertEqual(detect_intent(query)["activity_mode"], "single_venue")
                self.assertEqual(safe_intent(query)["categories"], ["computer_club"])
        for negative in ("Не хочу в компьютерный клуб, хочу кофе", "not a computer club, want a cafe", "без ПК-клуба, хочу кофе", "Не компьютерный клуб, а кофе рядом с Байтереком"):
            with self.subTest(negative=negative):
                self.assertEqual(safe_intent(negative)["categories"], ["cafe"])
                self.assertEqual(detect_intent(negative)["preferred_categories"], ["cafe"])
                self.assertIn("computer_club", detect_intent(negative)["excluded_categories"])
        self.assertFalse(activity_intent("Ночной клуб возле Байтерека")["categories"])

    def test_english_want_negation_preserves_museum_or_cafe_instead_of_club(self):
        for negative in ("don't want", "don’t want", "do not want"):
            for requested in ("museum", "cafe"):
                query = f"I {negative} a computer club, I want a {requested} near Baiterek"
                with self.subTest(negative=negative, requested=requested):
                    self.assertEqual(safe_intent(query)["categories"], [requested])
                    local = detect_intent(query)
                    self.assertEqual(local["preferred_categories"], [requested])
                    self.assertIn("computer_club", local["excluded_categories"])
                    self.assertFalse(activity_intent(query)["unsupported"])
        # A rejected activity in an earlier clause must not suppress the new one.
        positive = "I don't want coffee, I want a computer club near Baiterek"
        self.assertEqual(safe_intent(positive)["categories"], ["computer_club"])
        self.assertEqual(detect_intent(positive)["preferred_categories"], ["computer_club"])

    def test_known_venue_search_is_safe_and_unknown_explicit_venue_is_not_a_walk(self):
        query = "Я хочу в комп клуб на пару часов рядом с Байтереком PRIVATE_PERSON_ADDRESS"
        context, intent, text, _, params = prepare({"query": query, "minutes": 120, "time_of_day": "day"}, resolve_request)
        self.assertEqual(intent["categories"], ["computer_club"])
        self.assertIn("компьютерные клубы", text)
        self.assertNotIn("достопримечательности", text)
        self.assertNotIn("PRIVATE_PERSON_ADDRESS", json.dumps(params, ensure_ascii=False))
        self.assertNotIn(query, json.dumps(params, ensure_ascii=False))
        self.assertEqual(context["budget_options"], [120])
        for query in ("Хочу в боулинг рядом с Байтереком", "Хочу на батуты рядом с Mega", "find a bowling alley near Mega"):
            with self.subTest(query=query), self.assertRaises(GoogleError) as raised:
                safe_intent(query)
            self.assertEqual(raised.exception.code, "unsupported_activity")

    def test_negated_primary_club_and_generic_requests_keep_their_actual_goal(self):
        for query, expected in (("Я хочу не в компьютерный клуб, а в музей рядом с Байтереком", "museum"),
                                ("Хочу не в ПК-клуб, а в кафе возле Байтерека", "cafe")):
            with self.subTest(query=query):
                self.assertEqual(safe_intent(query)["categories"], [expected])
                self.assertEqual(detect_intent(query)["preferred_categories"], [expected])
                self.assertIn("computer_club", detect_intent(query)["excluded_categories"])
        for query in ("найти где погулять возле Mega", "найти хорошее кафе возле Mega",
                      "хочу в тихое кафе возле Mega", "Найти интересное место возле Mega", "find a place near Mega"):
            with self.subTest(query=query):
                self.assertFalse(activity_intent(query)["unsupported"])
                self.assertTrue(safe_intent(query)["categories"])

    def test_unknown_primary_venue_cannot_be_replaced_by_supported_secondary_coffee(self):
        for query in ("Хочу в боулинг и кофе рядом с Байтереком", "Хочу в боулинг рядом с кафе у Mega",
                      "Хочу в интересный боулинг рядом с Байтереком", "find an interesting bowling alley near Mega"):
            with self.subTest(query=query):
                self.assertTrue(detect_intent(query)["unsupported_activity"])
                with self.assertRaises(GoogleError) as raised:
                    safe_intent(query)
                self.assertEqual(raised.exception.code, "unsupported_activity")

    def test_retrieval_cannot_replace_missing_gaming_venue_with_a_nearby_park(self):
        origin = {"lat": 51.1283, "lon": 71.4305}
        park = poi("osm:node/1", "Парк у старта", 51.1284, 71.4306)
        club = poi("osm:node/2", "Синтетический компьютерный клуб", 51.130, 71.433, "computer_club")
        query = "Я хочу в комп клуб рядом с Байтереком"
        for places, expected in (([park], []), ([park, club], [club["id"]])):
            result = Retriever(data={"dataset": {"is_demo": True}, "places": places}).retrieve(query, origin, 120)
            self.assertEqual([item["poi"]["id"] for item in result["selected"]], expected)


class SingleVenueWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.google = Google()
        self.osm, self.dgis, self.narrator = Mock(), Mock(), Mock()
        self.osm.retrieve.return_value = {"selected": [{"poi": poi("osm:node/1", "Нерелевантный парк", 51.1284, 71.4306)}]}
        self.dgis.status.return_value = {"available": True}
        self.dgis.search.return_value = {"places": [], "warnings": []}
        self.grounding = {"summary": "Полный исходный ответ [0].", "places": [{
            "id": "google-club-title-only", "place": "places/google-club-title-only",
            "location": {"latitude": 51.131, "longitude": 71.433},
            "attribution": {"title": "Computer Club - Google Maps", "url": "https://www.google.com/maps/?cid=1"},
            "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/?cid=1"}}]}
        self.google.search_places = Mock(return_value=deepcopy(self.grounding))
        self.narrator.return_value = ({"text": "Синтетический цельный ответ про компьютерный клуб.", "source_ids": ["d0"], "stop_ids": ["dgis:2gis:1"]}, "mock_validated")
        self.service = UnifiedWalks(resolve_request, self.osm, self.google, self.dgis, narrator=self.narrator)
        self.form = {"query": "Я хочу в комп клуб на пару часов рядом с Байтереком", "minutes": 120, "time_of_day": "day", "consent": True}

    def test_all_sources_target_club_but_only_relevant_evidence_can_make_one_stop(self):
        club = poi("2gis:1", "Компьютерный клуб из рубрики", 51.129, 71.432, "computer_club")
        farther = poi("2gis:2", "Другой компьютерный клуб", 51.132, 71.433, "computer_club")
        self.dgis.search.return_value["places"] = [club, farther, poi("2gis:3", "Парк", 51.1284, 71.4306)]
        before = deepcopy(self.grounding)
        result = self.service.recommend(self.form)
        self.assertIn("компьютерные клубы", self.google.search_places.call_args.args[0]["textQuery"])
        self.assertEqual(self.osm.retrieve.call_args.args[0], self.form["query"])
        self.assertEqual(self.dgis.search.call_args.args[0], self.form["query"])
        self.assertEqual(result["source_receipt"]["osm"]["candidate_count"], 0)
        self.assertEqual(result["source_receipt"]["google"]["candidate_count"], 0)
        self.assertEqual(result["source_receipt"]["dgis"]["candidate_count"], 2)
        option = result["alternatives"][0]
        self.assertEqual(option["budget_minutes"], 120)
        self.assertEqual(len(option["stops"]), 1)
        self.assertEqual(option["stops"][0]["category"], "computer_club")
        self.assertEqual(option["plan"]["total_minutes"], 120)
        self.assertEqual(option["plan"]["visit_minutes"], 120-option["plan"]["walk_minutes"]-5)
        self.assertTrue(option["plan"]["visit_is_proposal"])
        self.assertEqual(len(option["plan"]["legs"]), 2)
        self.assertEqual(len(self.google.legs), 4)
        self.assertTrue(all(start=={"latLng": {"latitude": 51.1283, "longitude": 71.4305}} for start, _ in self.google.legs[::2]))
        self.assertEqual(self.narrator.call_count, 1)
        self.assertEqual(self.narrator.call_args.args[4]["categories"], ["computer_club"])
        self.assertEqual(result["grounding"]["summary"], before["summary"])
        self.assertEqual(len(result["grounding"]["sources"]), len(before["places"]))
        self.assertEqual(self.grounding, before)

    def test_no_matching_club_means_no_substitute_route_or_generation(self):
        self.dgis.search.return_value["places"] = [poi("2gis:1", "Близкий парк", 51.1284, 71.4306)]
        result = self.service.recommend(self.form)
        self.assertEqual(self.google.legs, [])
        self.narrator.assert_not_called()
        self.assertIsNone(result["answer"])
        self.assertEqual(result["diagnostics"]["llm_attempts"], 0)
        self.assertEqual(result["alternatives"][0]["stops"], [])
        self.assertEqual(result["alternatives"][0]["budget_status"], "no_candidates")
        self.assertEqual(result["grounding"]["summary"], self.grounding["summary"])

    def test_unknown_activity_is_rejected_before_any_source_or_model_call(self):
        with self.assertRaises(GoogleError) as raised:
            self.service.recommend({**self.form, "query": "Хочу в боулинг и кофе рядом с Байтереком"})
        self.assertEqual(raised.exception.code, "unsupported_activity")
        self.osm.retrieve.assert_not_called()
        self.dgis.search.assert_not_called()
        self.google.search_places.assert_not_called()
        self.narrator.assert_not_called()

    def test_unrouteable_nearest_club_does_not_hide_a_valid_alternative(self):
        clubs = [poi("2gis:1", "Первый клуб", 51.129, 71.432, "computer_club"), poi("2gis:2", "Второй клуб", 51.132, 71.433, "computer_club")]
        self.dgis.search.return_value["places"] = clubs
        original = self.google.compute_route
        def route(start, finish):
            if finish.get("latLng", {}).get("latitude") == clubs[0]["lat"]:
                self.google.legs.append((start, finish))
                raise GoogleError("google_timeout", "Synthetic failure", 504)
            return original(start, finish)
        self.google.compute_route = route
        result = self.service.recommend(self.form)
        self.assertEqual([stop["id"] for stop in result["alternatives"][0]["stops"]], ["2gis:2"])
        self.assertEqual(len(self.google.legs), 4)
        self.assertTrue(result["alternatives"][0]["plan"]["returns_to_origin"])


if __name__ == "__main__":
    unittest.main()
