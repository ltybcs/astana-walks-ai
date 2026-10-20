"""Unified narration contract tests. HTTP/model responses are in-memory fakes."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.google_client import json_bytes, strict_json
from app.llm import LLMConfig
from app.unified_narration import narrate


class Response:
    def __init__(self, value=None, *, raw=None):
        self.raw = raw if raw is not None else json_bytes(value)

    def read(self, size=-1):
        return self.raw[:size] if size >= 0 else self.raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class UnifiedNarrationTests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig("http://127.0.0.1:8081/v1", "mock-local", "", 120, "schema", "max_tokens")
        self.context = {"query": "PRIVATE_ORIGINAL_QUERY", "time_of_day": "day",
                        "sensitive_origin": True, "origin_id": "device_geolocation",
                        "origin": {"lat": 51.080123, "lon": 71.400123, "label": "PRIVATE_START_ADDRESS"}}
        self.intent = {"categories": ["walk"], "preferences": ["quiet"], "raw_query": "PRIVATE_RAW_INTENT"}
        self.grounding = {"summary": "Полный исходный контекст [0], [1], [2], [3], [4].",
                          "sources": [{"id": "google-place-" + str(i), "source_id": "g" + str(i),
                                       "title": "Google источник " + str(i),
                                       "url": "https://www.google.com/maps?place_id=google-place-" + str(i),
                                       "attribution": {"provider": "Google Maps"},
                                       "googleMapsLinks": {"directionsUrl": "PRIVATE_UNUSED_ACTION_URL"}}
                                      for i in range(5)]}
        self.stops = [
            {"stop_id": "osm:node/123", "id": "node/123", "source_id": "o0", "provider": "osm",
             "name": "OSM Сад", "category": "park", "description": "Зелёная территория из OSM.",
             "features": {"socket": "unknown", "quiet": "unknown", "wheelchair": "unknown"},
             "opening_hours": None, "source_url": "https://www.openstreetmap.org/node/123",
             "lat": 51.090987, "lon": 71.409876, "visit_minutes": 15},
            {"stop_id": "dgis:opaque-7", "id": "opaque-7", "source_id": "d0", "provider": "dgis",
             "name": "Сквер 2ГИС", "category": "park", "description": "Сквер из ответа 2ГИС.",
             "features": {}, "opening_hours": None, "source_url": "https://2gis.kz/astana/firm/opaque-7",
             "lat": 51.091234, "lon": 71.401234, "visit_minutes": 15},
            {"stop_id": "google:google-place-2", "id": "google-place-2", "source_id": "g2",
             "provider": "google", "name": "Google источник 2", "category": "unknown",
             "description": "", "features": {}, "opening_hours": None,
             "source_url": "https://www.google.com/maps?place_id=google-place-2",
             "lat": 51.091111, "lon": 71.411111, "visit_minutes": 15},
        ]
        self.alternatives = [
            {"id": "budget-60", "budget_minutes": 60, "route_built": True, "budget_status": "fits",
             "plan": {"budget_minutes": 60, "total_minutes": 50, "walk_minutes": 30,
                      "visit_minutes": 15, "buffer_minutes": 5, "return_walk_minutes": 14,
                      "verified": True, "returns_to_origin": True,
                      "route_attributions": [{"title": "PRIVATE_ROUTE_TITLE", "url": "PRIVATE_ROUTE_URL"}],
                      "legs": [{"from_ref": "PRIVATE_START_ADDRESS", "to_ref": "osm:node/123", "minutes": 16}]},
             "stops": self.stops[:1]},
            {"id": "budget-120", "budget_minutes": 120, "route_built": True, "budget_status": "fits",
             "plan": {"budget_minutes": 120, "total_minutes": 90, "walk_minutes": 40,
                      "visit_minutes": 45, "buffer_minutes": 5, "return_walk_minutes": 12,
                      "verified": True, "returns_to_origin": True},
             "stops": self.stops},
        ]
        self.answer = {"text": "Предлагаю прогулку у выбранных мест по вашему интересу. "
                               "Идея: осмотреть их облик снаружи и сделать паузу. "
                               "Условия посещения неизвестны.",
                       "stop_ids": [stop["stop_id"] for stop in self.stops],
                       "source_ids": ["o0", "d0", "g2"]}
        self.calls = []

    def model(self, answer=None, *, raw=None):
        result = deepcopy(self.answer if answer is None else answer)
        def opened(request, timeout):
            self.calls.append((strict_json(request.data), request.full_url, timeout))
            if raw is not None:
                return Response(raw=raw)
            return Response({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
                                           "finish_reason": "stop"}]})
        return opened

    def run_narrator(self, **overrides):
        arguments = {"grounding": self.grounding, "stops": self.stops, "alternatives": self.alternatives,
                     "context": self.context, "intent": self.intent, "config": self.config,
                     "http_open": self.model()}
        arguments.update(overrides)
        # Any accidentally omitted mock must fail offline, never reach localhost.
        with patch("app.unified_narration.open_local_model", side_effect=AssertionError("Unexpected model HTTP")):
            return narrate(**arguments)

    def test_one_combined_payload_keeps_google_whole_and_provider_facts_separate(self):
        original = deepcopy((self.grounding, self.stops, self.alternatives, self.context, self.intent))
        answer, status = self.run_narrator()
        self.assertEqual(answer, self.answer)
        self.assertEqual(status, "validated_structure_sources_and_known_claims")
        self.assertEqual(len(self.calls), 1)
        payload, url, timeout = self.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8081/v1/chat/completions")
        self.assertEqual(timeout, 120)
        self.assertIs(payload["cache_prompt"], False)
        context = strict_json(payload["messages"][1]["content"])
        self.assertEqual(context["grounding_lite_summary"], self.grounding["summary"])
        self.assertEqual(context["all_google_source_previews"],
                         [{key: source[key] for key in ("id", "source_id", "title", "url", "attribution")}
                          for source in self.grounding["sources"]])
        self.assertEqual([fact["provider"] for fact in context["selected_provider_facts"]],
                         ["osm", "dgis", "google"])
        self.assertEqual(context["selected_provider_facts"][0]["description"], self.stops[0]["description"])
        self.assertEqual(context["selected_provider_facts"][1]["description"], self.stops[1]["description"])
        self.assertEqual(context["selected_provider_facts"][0]["features"]["quiet"], "unknown")
        self.assertEqual(context["selected_provider_facts"][2]["category"], "unknown")
        self.assertEqual(context["required_source_ids"], ["o0", "d0", "g2"])
        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["source_ids"]["items"]["enum"], ["o0", "d0", "g2"])
        self.assertEqual(schema["properties"]["stop_ids"]["minItems"], 3)
        wire = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("PRIVATE_", "51.080123", "71.400123", "51.090987", "71.409876",
                          "openstreetmap.org", "2gis.kz", "route_attributions", "legs"):
            self.assertNotIn(forbidden, wire)
        self.assertEqual((self.grounding, self.stops, self.alternatives, self.context, self.intent), original)

    def test_narration_plan_projection_omits_arithmetic_but_keeps_guard_and_sources(self):
        # These are valid planning values, not permission for a prose timing claim.
        self.alternatives[1].update(id="budget-300", budget_minutes=300)
        self.alternatives[1]["plan"].update(budget_minutes=300, total_minutes=300,
            walk_minutes=20, visit_minutes=275, buffer_minutes=5, return_walk_minutes=10)
        original = deepcopy((self.alternatives, self.grounding))
        answer, status = self.run_narrator()
        self.assertEqual(answer, self.answer)
        self.assertEqual(status, "validated_structure_sources_and_known_claims")
        sent = strict_json(self.calls[0][0]["messages"][1]["content"])
        for item, option in zip(sent["alternatives"], self.alternatives):
            self.assertEqual(set(item), {"budget_status", "stop_ids", "verified", "returns_to_origin"})
            self.assertEqual(item["budget_status"], option["budget_status"])
            self.assertEqual(item["stop_ids"], [stop["stop_id"] for stop in option["stops"]])
            self.assertIs(item["verified"], True)
            self.assertIs(item["returns_to_origin"], True)
            self.assertNotIn("275", json.dumps(item))
            self.assertNotIn("300", json.dumps(item))
        self.assertEqual(sent["grounding_lite_summary"], self.grounding["summary"])
        self.assertEqual(sent["all_google_source_previews"], [
            {key: source[key] for key in ("id", "source_id", "title", "url", "attribution")}
            for source in self.grounding["sources"]])
        self.assertEqual((self.alternatives, self.grounding), original)
        value = {**self.answer, "venue_descriptions": [
            {"stop_id": stop["stop_id"], "text": "Предлагаю короткую прогулку после проверки условий посещения."}
            for stop in self.stops]}
        value["venue_descriptions"][0]["text"] = "Предлагаю остановиться на 275 минут. Условия посещения неизвестны."
        self.calls.clear()
        answer, status = self.run_narrator(http_open=self.model(value))
        self.assertIsNone(answer)
        self.assertEqual(status, "venue_description_unsupported_number")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.alternatives, self.grounding), original)

    def test_surrounding_whitespace_keeps_the_whole_answer_verbatim(self):
        for prefix, suffix in ((" ", " "), ("\n", " "), ("", " ")):
            with self.subTest(prefix=prefix, suffix=suffix):
                self.calls.clear()
                value = {**self.answer, "text": prefix + self.answer["text"] + suffix}
                original = deepcopy(value)
                answer, reason = self.run_narrator(http_open=self.model(value))
                self.assertEqual(answer, original)
                self.assertEqual(answer["text"], original["text"])
                self.assertEqual(reason, "validated_structure_sources_and_known_claims")
                self.assertEqual(len(self.calls), 1)
        value = {**self.answer, "text": " " * 40}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertIsNone(answer)
        self.assertEqual(reason, "invalid_text")

    def test_unverified_quality_opinions_reject_whole_but_proper_names_are_exempt(self):
        for claim in ("хорошей репутацией", "уютное", "популярное", "вкусное", "отличное",
                      "великолепное", "прекрасное", "лучшее", "идеальное", "живописное", "комфортное"):
            with self.subTest(claim=claim):
                self.calls.clear()
                value = {**self.answer, "text": "Выбрано " + claim + " место. Идея: осмотреть его облик. Условия посещения неизвестны. "}
                answer, reason = self.run_narrator(http_open=self.model(value))
                self.assertIsNone(answer)
                self.assertEqual(reason, "unsupported_quality_claim")
                self.assertEqual(len(self.calls), 1)
        self.stops[0]["name"] = "Уютный сад"
        value = {**self.answer, "text": "Уютный сад подходит по категории для прогулки. Идея: осмотреть его облик снаружи. Условия посещения неизвестны. "}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(reason, "validated_structure_sources_and_known_claims")

    def test_known_unselected_google_venue_rejects_whole_despite_valid_selected_ids(self):
        self.grounding["sources"][4]["title"] = "Kul'tura Diktuyet - Google Maps"
        original = deepcopy(self.grounding)
        text = "Выбранные места подходят для прогулки. Идея: сделать паузу у Kul’tura Diktuyet. Условия посещения неизвестны."
        answer, reason = self.run_narrator(http_open=self.model({**self.answer, "text": text}))
        self.assertIsNone(answer)
        self.assertEqual(reason, "unselected_google_venue")
        self.assertEqual(len(self.calls), 1)
        sent = strict_json(self.calls[0][0]["messages"][1]["content"])
        self.assertEqual(sent["grounding_lite_summary"], original["summary"])
        self.assertEqual(len(sent["all_google_source_previews"]), 5)
        for sent_source, source in zip(sent["all_google_source_previews"], original["sources"]):
            for key in ("id", "source_id", "title", "url", "attribution"):
                self.assertEqual(sent_source[key], source[key])
        self.assertEqual(self.grounding, original)

    def test_selected_google_title_and_selected_longer_name_remain_allowed(self):
        self.stops[2]["name"] = "Selected Coffee - Google Maps"
        self.grounding["sources"][2]["title"] = self.stops[2]["name"]
        self.stops[1]["name"] = "Cult Coffee"
        self.grounding["sources"][4]["title"] = "Cult - Google Maps"
        text = "Выбранные места подходят для прогулки. Идея: осмотреть Selected Coffee и Cult Coffee снаружи. Условия посещения неизвестны."
        value = {**self.answer, "text": text}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(reason, "validated_structure_sources_and_known_claims")

    def test_selected_short_name_does_not_hide_a_longer_unselected_google_venue(self):
        self.stops[1]["name"] = "Cult"
        self.grounding["sources"][4]["title"] = "Cult Coffee - Google Maps"
        value = {**self.answer, "text": "Выбранные места подходят для прогулки. Идея: осмотреть Cult Coffee снаружи. Условия посещения неизвестны."}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertIsNone(answer)
        self.assertEqual(reason, "unselected_google_venue")
        self.assertEqual(len(self.calls), 1)

    def test_all_selected_ids_required_exact_no_extra_sources_or_stops(self):
        for key, values in (
            ("source_ids", ["o0", "d0"]), ("source_ids", ["o0", "d0", "g2", "g0"]),
            ("source_ids", ["o0", "d0", "g2", "g2"]),
            ("stop_ids", [self.stops[0]["stop_id"]]),
            ("stop_ids", [stop["stop_id"] for stop in self.stops] + ["google:other"]),
        ):
            with self.subTest(key=key, values=values):
                bad = deepcopy(self.answer)
                bad[key] = values
                self.calls.clear()
                answer, status = self.run_narrator(http_open=self.model(bad))
                self.assertIsNone(answer)
                self.assertEqual(status, "invalid_" + key)
                self.assertEqual(len(self.calls), 1)

    def test_arbitrary_html_urls_numbers_and_unknown_inline_source_reject_whole(self):
        cases = [
            ("Идея: <b>осмотреть место</b>. Условия посещения неизвестны.", "invalid_text"),
            ("Идея: осмотреть место https://evil.invalid. Условия посещения неизвестны.", "model_supplied_url"),
            ("Идея: пройти 999 метров. Условия посещения неизвестны.", "unsupported_number"),
            ("Идея: идти пять минут. Условия посещения неизвестны.", "unsupported_number"),
            ("Идея: остановиться на несколько минут. Условия посещения неизвестны.", "unsupported_number"),
            ("Идея: осмотреть выбранное место [g4]. Условия посещения неизвестны.", "unknown_inline_citation"),
            ("Идея: осмотреть выбранное место [d99]. Условия посещения неизвестны.", "unknown_inline_citation"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                bad = {**self.answer, "text": text}
                self.calls.clear()
                result, status = self.run_narrator(http_open=self.model(bad))
                self.assertIsNone(result)
                self.assertEqual(status, expected)
                self.assertEqual(len(self.calls), 1)

    def test_unknown_protected_facts_are_not_inferred_from_other_provider(self):
        for text in ("Здесь тихо и безопасно. Идея: осмотреть место. Условия посещения неизвестны.",
                     "Здесь есть розетки. Идея: осмотреть место. Условия посещения неизвестны.",
                     "Все места открыты сейчас. Идея: осмотреть место. Условия посещения неизвестны."):
            with self.subTest(text=text):
                bad = {**self.answer, "text": text}
                result, reason = self.run_narrator(http_open=self.model(bad))
                self.assertIsNone(result)
                self.assertTrue(reason.startswith("unsupported_protected_"), reason)

    def test_exact_selected_provider_label_is_not_a_number_but_nearby_claims_still_are(self):
        text = "Места выбраны по данным OpenStreetMap, Google Maps и 2ГИС. Идея: осмотреть их облик снаружи. Условия посещения неизвестны. "
        value = {**self.answer, "text": text}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(reason, "validated_structure_sources_and_known_claims")
        for claim in ("2ГИС: идти 999 метров.", "2ГИС: идти пять минут.", "2ГИС999 предлагает прогулку."):
            with self.subTest(claim=claim):
                value = {**self.answer, "text": claim + " Идея: осмотреть место. Условия посещения неизвестны."}
                answer, reason = self.run_narrator(http_open=self.model(value))
                self.assertIsNone(answer)
                self.assertEqual(reason, "unsupported_number")
        self.stops = self.stops[:1]
        self.alternatives = self.alternatives[:1]
        value = {"text": text, "stop_ids": [self.stops[0]["stop_id"]], "source_ids": ["o0"]}
        answer, reason = self.run_narrator(http_open=self.model(value))
        self.assertIsNone(answer)
        self.assertEqual(reason, "unsupported_number")

    def test_literal_source_name_can_have_numbers_and_protected_words(self):
        self.stops[0]["name"] = "Тихий сад 42"
        text = "Предлагаю осмотреть Тихий сад 42 [o0] и другие выбранные места. "
        text += "Идея: сделать паузу снаружи. Условия посещения неизвестны."
        answer, reason = self.run_narrator(http_open=self.model({**self.answer, "text": text}))
        self.assertEqual(answer["text"], text)
        self.assertEqual(reason, "validated_structure_sources_and_known_claims")

    def test_private_origin_echo_rejects_before_model_without_mutating_google(self):
        for target in ("summary", "preview"):
            with self.subTest(target=target):
                grounding = deepcopy(self.grounding)
                if target == "summary":
                    grounding["summary"] += " PRIVATE_START_ADDRESS"
                else:
                    grounding["sources"][4]["title"] += " 51.080123"
                original = deepcopy(grounding)
                self.calls.clear()
                result, status = self.run_narrator(grounding=grounding)
                self.assertIsNone(result)
                self.assertEqual(status, "sensitive_origin_in_model_context")
                self.assertEqual(self.calls, [])
                self.assertEqual(grounding, original)

    def test_private_origin_in_model_output_rejects_whole_no_retry(self):
        bad = {**self.answer, "text": "Предлагаю пройти от PRIVATE_START_ADDRESS к выбранным местам. "
                                     "Идея: осмотреть их облик. Условия посещения неизвестны."}
        result, status = self.run_narrator(http_open=self.model(bad))
        self.assertIsNone(result)
        self.assertEqual(status, "sensitive_origin_in_model_output")
        self.assertEqual(len(self.calls), 1)

    def test_local_only_config_rejects_external_key_or_external_origin_before_http(self):
        for config in (replace(self.config, base_url="https://example.invalid/v1"),
                       replace(self.config, api_key="PRIVATE_KEY"),
                       replace(self.config, base_url="http://127.0.0.1:8081/v1?query=secret")):
            with self.subTest(config=config.base_url):
                result, status = self.run_narrator(config=config)
                self.assertIsNone(result)
                self.assertEqual(status, "local_model_unconfigured")
                self.assertEqual(self.calls, [])

    def test_no_verified_route_means_zero_model_calls(self):
        for option in self.alternatives:
            option["route_built"] = False
            option["budget_status"] = "unverified"
            option["plan"]["verified"] = False
        result, status = self.run_narrator()
        self.assertIsNone(result)
        self.assertEqual(status, "route_not_budget_verified")
        self.assertEqual(self.calls, [])

    def test_mixed_unverified_option_cannot_be_claimed_to_fit(self):
        self.alternatives[1]["route_built"] = False
        self.alternatives[1]["budget_status"] = "unverified"
        self.alternatives[1]["plan"]["verified"] = False
        bad = {**self.answer, "text": "Все прогулки укладываются в бюджет. "
                                     "Идея: осмотреть выбранные места. Условия посещения неизвестны."}
        result, status = self.run_narrator(http_open=self.model(bad))
        self.assertIsNone(result)
        self.assertEqual(status, "unsupported_budget_fit")

    def test_incomplete_json_and_oversized_responses_have_no_repair_or_cache(self):
        for raw, expected in (
            (json_bytes({"choices": [{"message": {"content": '{"text":"неполный'}}]}),
             "local_model_unavailable_or_invalid"),
            (b"x" * 100001, "model_response_too_large"),
        ):
            with self.subTest(expected=expected):
                self.calls.clear()
                result, status = self.run_narrator(http_open=self.model(raw=raw))
                self.assertIsNone(result)
                self.assertEqual(status, expected)
                self.assertEqual(len(self.calls), 1)

    def test_missing_selected_google_source_and_unbounded_context_fail_before_model(self):
        self.stops[2]["source_id"] = "g99"
        result, status = self.run_narrator()
        self.assertIsNone(result)
        self.assertEqual(status, "selected_source_missing")
        self.assertEqual(self.calls, [])
        self.stops[2]["source_id"] = "g2"
        self.grounding["summary"] = "x" * 60001
        result, status = self.run_narrator()
        self.assertIsNone(result)
        self.assertEqual(status, "context_exceeds_local_bound")
        self.assertEqual(self.calls, [])

    def test_cafe_action_guard_retained_for_combined_output(self):
        self.intent["categories"] = ["cafe"]
        bad = {**self.answer, "text": "Предлагаю остановиться у выбранных мест. "
                                     "Идея: осмотреть фасад здания. Условия посещения неизвестны."}
        result, status = self.run_narrator(http_open=self.model(bad))
        self.assertIsNone(result)
        self.assertEqual(status, "action_does_not_match_cafe_intent")


    def test_computer_club_intent_survives_model_context_without_outdoor_substitution(self):
        self.intent["categories"] = ["computer_club"]
        for stop in self.stops:
            stop["category"] = "computer_club"
        good = {**self.answer, "text": "Компьютерный клуб соответствует выбранному занятию. "
                                     "Предлагаю запланировать игровую паузу после проверки условий. "
                                     "Условия посещения неизвестны."}
        result, status = self.run_narrator(http_open=self.model(good))
        self.assertEqual(result, good)
        payload = self.calls[-1][0]
        context = json.loads(payload["messages"][1]["content"])
        self.assertEqual(context["intent"]["categories"], ["computer_club"])
        self.assertEqual(context["grounding_lite_summary"], self.grounding["summary"])
        self.assertEqual(len(context["all_google_source_previews"]), 5)
        bad = {**good, "text": "Выбранное место подходит для отдыха. Предлагаю прогулку по парку. Условия посещения неизвестны."}
        result, status = self.run_narrator(http_open=self.model(bad))
        self.assertIsNone(result)
        self.assertEqual(status, "action_does_not_match_computer_club_intent")


    def test_unknown_club_equipment_or_current_capacity_rejects_whole_answer(self):
        self.intent["categories"] = ["computer_club"]
        for stop in self.stops:
            stop.update(category="computer_club", features={}, opening_hours=None)
        for assertion, expected in (
            ("В компьютерном клубе есть мощные игровые компьютеры.", "equipment"),
            ("В компьютерном клубе есть свободные места.", "availability"),
            ("Клуб оснащён игровыми компьютерами.", "equipment"),
            ("Свободные места есть, но условия неизвестны.", "availability"),
            ("Свободных мест нет.", "availability"),
        ):
            with self.subTest(assertion=assertion):
                self.calls.clear()
                value = {**self.answer, "text": assertion + " Предлагаю запланировать игровую паузу. Условия посещения неизвестны."}
                original = deepcopy(value)
                answer, status = self.run_narrator(http_open=self.model(value))
                self.assertIsNone(answer)
                self.assertEqual(status, "unsupported_computer_club_" + expected)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(value, original)

    def test_club_equipment_advice_unknown_and_source_identity_are_not_claims(self):
        self.intent["categories"] = ["computer_club"]
        for stop in self.stops:
            stop.update(category="computer_club", features={}, opening_hours=None)
        for statement in ("Уточните наличие мощных игровых компьютеров и свободных мест.",
                          "Наличие компьютеров неизвестно. Наличие свободных мест не подтверждено.",
                          "Уточните, есть ли компьютеры. Если есть свободные места, проверьте условия."):
            with self.subTest(statement=statement):
                value = {**self.answer, "text": "Компьютерный клуб соответствует выбранному занятию. " + statement + " Предлагаю запланировать игровую паузу. Условия посещения неизвестны."}
                answer, status = self.run_narrator(http_open=self.model(value))
                self.assertEqual(answer, value)
                self.assertEqual(status, "validated_structure_sources_and_known_claims")
        # A literal source name is identity, not proof or an assertion of capacity.
        self.stops[1]["name"] = "Компьютеры и свободные места"
        value = {**self.answer, "text": "Выбранный источник «Компьютеры и свободные места» соответствует интересу к компьютерному клубу. Предлагаю запланировать игровую паузу. Условия посещения неизвестны."}
        answer, status = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(status, "validated_structure_sources_and_known_claims")


    def test_coordinated_club_unknown_subjects_share_only_their_own_predicate(self):
        self.intent["categories"] = ["computer_club"]
        for stop in self.stops:
            stop.update(category="computer_club", features={}, opening_hours=None)
        for statement in ("Наличие игровых компьютеров и свободных мест не подтверждено.",
                          "Наличие свободных мест и игровых компьютеров не подтверждено.",
                          "Игровые компьютеры и свободные места неизвестны.",
                          "Наличие свободных мест и мощных игровых компьютеров пока неизвестно."):
            with self.subTest(statement=statement):
                self.calls.clear()
                value = {**self.answer, "text": statement + " Предлагаю игровую паузу в компьютерном клубе."}
                answer, status = self.run_narrator(http_open=self.model(value))
                self.assertEqual(answer, value)
                self.assertEqual(status, "validated_structure_sources_and_known_claims")
                self.assertEqual(len(self.calls), 1)
        for statement, expected in (
            ("Есть компьютеры; наличие свободных мест не подтверждено.", "equipment"),
            ("Есть свободные места; наличие игровых компьютеров неизвестно.", "availability"),
            ("Есть компьютеры и свободные места. Условия посещения неизвестны.", "equipment"),
            ("Наличие игровых компьютеров и свободных мест не подтверждено, но есть компьютеры.", "equipment"),
        ):
            with self.subTest(statement=statement):
                self.calls.clear()
                value = {**self.answer, "text": statement + " Предлагаю игровую паузу в компьютерном клубе."}
                answer, status = self.run_narrator(http_open=self.model(value))
                self.assertIsNone(answer)
                self.assertEqual(status, "unsupported_computer_club_" + expected)
                self.assertEqual(len(self.calls), 1)


    def test_per_venue_descriptions_are_whole_accepted_scoped_and_not_rewritten(self):
        value = deepcopy(self.answer)
        value["venue_descriptions"] = [
            {"stop_id": stop["stop_id"], "text": "  " + stop["name"] + ": запланируйте короткую прогулку и паузу после проверки условий. Условия посещения неизвестны.  "}
            for stop in self.stops]
        answer, status = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(status, "validated_structure_sources_and_known_claims")
        payload = self.calls[-1][0]
        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertIn("venue_descriptions", schema["required"])
        self.assertLessEqual(payload[self.config.token_parameter], 950)
        context = strict_json(payload["messages"][1]["content"])
        self.assertEqual(context["grounding_lite_summary"], self.grounding["summary"])
        self.assertEqual(len(context["all_google_source_previews"]), 5)

    def test_per_venue_invalid_ids_cross_venue_claim_and_foreign_google_reject_whole(self):
        self.stops[1]["name"] = "Синтетический сквер"
        self.grounding["sources"][0]["title"] = "Чужой клуб - Google Maps"
        valid = [{"stop_id": stop["stop_id"], "text": "Здесь можно запланировать короткую прогулку и паузу после проверки условий посещения."}
                 for stop in self.stops]
        for change, expected in (("duplicate", "invalid_venue_descriptions"),
                                 ("foreign_selected", "cross_venue_description"),
                                 ("unselected_google", "venue_description_unselected_google_venue"),
                                 ("rating", "venue_description_unsupported_number")):
            self.calls.clear()
            entries = deepcopy(valid)
            if change == "duplicate": entries[1]["stop_id"] = entries[0]["stop_id"]
            if change == "foreign_selected": entries[0]["text"] += " Выбрано место " + self.stops[1]["name"] + "."
            if change == "unselected_google": entries[0]["text"] += " Предлагаю место Чужой клуб."
            if change == "rating": entries[0]["text"] += " Рейтинг 4.9."
            answer, status = self.run_narrator(http_open=self.model({**self.answer, "venue_descriptions": entries}))
            self.assertIsNone(answer)
            self.assertEqual(status, expected)
            self.assertEqual(len(self.calls), 1)

    def test_per_venue_equipment_and_quality_do_not_leak_from_other_google_context(self):
        self.stops = [{**self.stops[1], "name": "BRO", "category": "computer_club"}]
        for option in self.alternatives: option["stops"] = self.stops
        self.intent = {"categories": ["computer_club"], "preferences": []}
        base = {"text": "Предлагаю игровую паузу в компьютерном клубе после проверки условий. Условия посещения неизвестны.",
                "stop_ids": [self.stops[0]["stop_id"]], "source_ids": ["d0"]}
        for text in ("В BRO есть мощные игровые компьютеры. Предлагаю игровую паузу. Условия посещения неизвестны.",
                     "BRO отличается отличной репутацией. Предлагаю игровую паузу после проверки условий."):
            answer, status = self.run_narrator(http_open=self.model({**base, "venue_descriptions": [{"stop_id": self.stops[0]["stop_id"], "text": text}]}))
            self.assertIsNone(answer)
            self.assertTrue(status.startswith("venue_description_"))
        good = {**base, "venue_descriptions": [{"stop_id": self.stops[0]["stop_id"],
                    "text": "BRO: запланируйте игровую паузу после проверки условий посещения. Условия посещения неизвестны."}]}
        answer, _ = self.run_narrator(http_open=self.model(good))
        self.assertEqual(answer, good)


    def test_mixed_venue_descriptions_use_only_their_own_confirmed_type(self):
        self.stops = [self.stops[0], {**self.stops[1], "name": "Нейтральная кофейня",
                      "category": "cafe", "source_categories": ["cafe"]}]
        for option in self.alternatives: option["stops"] = self.stops
        self.intent = {"categories": ["park", "cafe"], "preferences": []}
        value = {"text": "Предлагаю прогулку с паузой на кофе у выбранных мест. Условия посещения неизвестны.",
                 "stop_ids": [stop["stop_id"] for stop in self.stops], "source_ids": ["o0", "d0"],
                 "venue_descriptions": [
                     {"stop_id": self.stops[0]["stop_id"], "text": "Это парк: запланируйте короткую прогулку после проверки условий посещения."},
                     {"stop_id": self.stops[1]["stop_id"], "text": "Это кафе: запланируйте паузу на кофе после проверки условий посещения."}]}
        answer, status = self.run_narrator(http_open=self.model(value))
        self.assertEqual(answer, value)
        self.assertEqual(status, "validated_structure_sources_and_known_claims")
        for index, text in ((0, "Это кафе: запланируйте паузу на кофе после проверки условий посещения."),
                            (0, "Это компьютерный клуб. Предлагаю игровую паузу после проверки условий."),
                            (1, "Это парк: запланируйте короткую прогулку после проверки условий посещения.")):
            with self.subTest(index=index, text=text):
                self.calls.clear()
                invalid = deepcopy(value)
                invalid["venue_descriptions"][index]["text"] = text
                answer, status = self.run_narrator(http_open=self.model(invalid))
                self.assertIsNone(answer)
                self.assertEqual(status, "venue_description_type_mismatch")
                self.assertEqual(len(self.calls), 1)
                context = strict_json(self.calls[0][0]["messages"][1]["content"])
                self.assertEqual(context["grounding_lite_summary"], self.grounding["summary"])
                self.assertEqual(len(context["all_google_source_previews"]), 5)


    def test_validated_plan_context_reaches_one_model_without_start_or_raw_quotes(self):
        from app.semantic_intent import validate_intent
        query = "Хочу прогулку возле PRIVATE_HOME"
        plan = validate_intent({"start_quote": "PRIVATE_HOME", "goals": [{"quote": "прогулку",
            "search_text": "короткая прогулка", "category_hint": "walk", "fulfilment": "generic_walk"}],
            "excluded": [], "goal_policy": "primary", "requirements": [], "context": []}, query)
        result, status = self.run_narrator(intent_plan=plan)
        self.assertEqual(result, self.answer)
        self.assertEqual(len(self.calls), 1)
        value = strict_json(self.calls[0][0]["messages"][1]["content"])
        self.assertEqual(value["intent"]["semantic_goal"], plan.sanitized_narration)
        self.assertEqual(value["grounding_lite_summary"], self.grounding["summary"])
        self.assertEqual(len(value["all_google_source_previews"]), 5)
        self.assertNotIn("PRIVATE_HOME", json.dumps(value))
        self.assertNotIn(query, json.dumps(value))


if __name__ == "__main__":
    unittest.main()
