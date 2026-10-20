"""Whole Google pipeline regressions. Every provider/model is an in-memory mock."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from app import google_lab as lab
from app.google_budget import GoogleBudget, GoogleError
from app.google_client import GoogleClient, json_bytes, strict_json
from app.google_grounded import GoogleGrounded, TokenSigner, validate_grounding, make_plan, narrate, _answer_rejection, protected_claim_rejection, _contains_private_origin
from app.llm import LLMConfig
from tests.test_google_client import Provider, Response
from tests import test_google_lab as lab_tests
running_server, request = lab_tests.running_server, lab_tests.request

FORM = {"query": "У меня 45 минут возле Mega, хочу короткую прогулку"}
GOOD_TEXT = "Предлагаю короткую прогулку у места из источника [0]. Идея: осмотреть окрестности и сделать паузу. Перед выходом проверьте условия посещения."


def good_answer():
    return {"text": GOOD_TEXT, "selected_place_ids": ["mock-place"], "source_ids": ["g0"]}


class GoogleGroundedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "test-ledger.json"
        GoogleBudget.initialize(self.path)
        self.provider = Provider()
        self.budget = GoogleBudget(self.path)
        self.client = GoogleClient("MOCK_KEY", self.budget, http_open=self.provider)
        self.config = LLMConfig("http://127.0.0.1:8081/v1", "mock-local", "", 120, "schema", "max_tokens")
        self.calls = []
        def narrator(*args):
            self.calls.append(deepcopy(args))
            return good_answer(), "mocked_validated"
        # Clock behavior is tested with injected time in test_lab_context; this
        # provider fixture fixes daytime so CI does not depend on wall clock.
        resolver = lambda payload: lab.resolve_request({"time_of_day": "day", **payload})
        self.service = GoogleGrounded(self.client, resolver, llm_config=self.config, narrator=narrator)

    def consent(self, form=None):
        form = deepcopy(form or FORM)
        preview = self.service.preview(form)
        return {**form, "consent": True, "preview_token": preview["preview_token"]}

    def test_natural_query_preview_reconstructs_public_safe_intent_no_network(self):
        query = "У меня 45 минут возле Mega, хочу тихий кофе и розетку. Меня зовут PRIVATE_MARKER_9821"
        preview = self.service.preview({"query": query})
        self.assertEqual(preview["resolved_context"]["origin_id"], "mega")
        self.assertEqual(preview["resolved_context"]["minutes"], 45)
        self.assertEqual(preview["structured_intent"], {"categories": ["cafe"], "preferences": ["quiet", "socket"]})
        self.assertIn("EXPO / Nur Alem рядом с MEGA Silk Way, Астана", preview["outbound_search_text"])
        self.assertEqual(preview["resolved_context"]["minutes"], 45)
        self.assertEqual(preview["resolved_context"]["time_of_day"], "day")
        self.assertNotIn("45", preview["outbound_search_text"])
        self.assertNotIn("day", preview["outbound_search_text"])
        self.assertIn("розетка только при явном подтверждении", preview["outbound_search_text"])
        self.assertEqual(preview["nearby_radius_meters"], 1050)
        self.assertEqual(preview["outbound_search_parameters"]["locationBias"],
                         {"circle": {"center": {"latitude": 51.0892, "longitude": 71.4074}, "radiusMeters": 1050}})
        self.assertEqual(preview["outbound_search_parameters"]["pageSize"], 5)
        self.assertIs(preview["outbound_search_parameters"]["includeExtendedDetails"], False)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(preview))
        self.assertNotIn(query, json.dumps(preview))
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)
        cases = [
            (FORM["query"], ["walk"]),
            ("хочу прогулку в парке", ["park"]),
            ("хочу прогуляться, детскую площадку или парк", ["park", "playground"]),
            ("хочу погулять с ребенком", ["playground"]),
            ("хочу кофе и погулять", ["walk", "cafe"]),
            ("walk in a park", ["park"]),
        ]
        for query, categories in cases:
            with self.subTest(query=query):
                preview = self.service.preview({"query": query, "origin_id": "mega", "minutes": 45})
                self.assertEqual(preview["structured_intent"]["categories"], categories)
                self.assertEqual(preview["nearby_radius_meters"], 1050)
                self.assertEqual(preview["outbound_search_parameters"]["pageSize"], 5)
                self.assertIs(preview["outbound_search_parameters"]["includeExtendedDetails"], False)
                if categories == ["walk"]:
                    self.assertEqual(preview["outbound_search_text"],
                                     "Прогулочные площади и архитектура на территории EXPO рядом с MEGA Silk Way, Астана. Nur Alem — только внешний осмотр.")
                    self.assertEqual(preview["resolved_context"]["minutes"], 45)
                    self.assertEqual(preview["resolved_context"]["time_of_day"], "day")
                    self.assertNotIn("парки", preview["outbound_search_text"])
                if "cafe" in categories:
                    self.assertIn("кафе для короткой паузы с кофе", preview["outbound_search_text"])
                    self.assertNotIn("архитектура", preview["outbound_search_text"])
                if categories == ["park"]:
                    self.assertIn("парки и сады", preview["outbound_search_text"])
                    self.assertNotIn("достопримечательности", preview["outbound_search_text"])
                    self.assertNotIn("архитектура", preview["outbound_search_text"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)

    def test_automatic_context_and_generic_intent_preview_have_no_network(self):
        for form in ({"query": "кофе"}, {"query": "хочу необычное рядом с Экспо 2017"},
                     {"query": "что посмотреть", "origin": {"lat": 51.0892, "lon": 71.4074, "label": "PRIVATE_HOME"}}):
            with self.subTest(form=form):
                preview = self.service.preview(form)
                self.assertEqual(preview["resolved_context"]["budget_options"], [60, 120, 240])
                self.assertEqual(preview["request_plan"]["max_model_calls"], 1)
                self.assertNotIn("PRIVATE_HOME", preview["outbound_search_text"])
        self.assertEqual(self.provider.requests, [])

    def test_consent_changed_form_expiry_tamper_scope_and_replay_before_network(self):
        payload = self.consent()
        for invalid in ({**payload, "consent": 1}, {**payload, "query": FORM["query"] + " пожалуйста"},
                        {**payload, "preview_token": payload["preview_token"] + "x"},
                        {**payload, "preview_token": self.service.signer.issue("selection", ["mock-place"])}):
            with self.subTest(invalid=invalid), self.assertRaises(GoogleError):
                self.service.recommend(invalid)
        self.assertEqual(self.provider.requests, [])
        self.service.recommend(payload)
        before = self.budget.status()["used"]
        with self.assertRaises(GoogleError):
            self.service.recommend(payload)
        self.assertEqual(self.budget.status()["used"], before)
        clock = [0]
        signer = TokenSigner(clock=lambda: clock[0], secret=b"x" * 32)
        token = signer.issue("preview", "digest")
        clock[0] = 601
        with self.assertRaises(GoogleError):
            signer.verify(token, "preview")

    def test_nonce_consume_is_atomic_and_process_restart_invalidates(self):
        signer = TokenSigner()
        token = signer.issue("preview", "digest")
        def consume(_):
            try:
                signer.verify(token, "preview", consume=True)
                return True
            except GoogleError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(consume, range(16))), 1)
        with self.assertRaises(GoogleError):
            TokenSigner().verify(token, "preview")

    def test_fixed_graph_two_distinct_walk_legs_whole_context_and_no_persistence(self):
        before = set(self.path.parent.iterdir())
        consent = self.consent()
        expected_parameters = self.service.preview(FORM)["outbound_search_parameters"]
        result = self.service.recommend(consent)
        self.assertEqual(result["mode"], "google-grounded-llm")
        self.assertEqual(result["answer"]["text"], GOOD_TEXT)
        self.assertEqual(result["grounding"]["summary"], self.provider.search["summary"])
        self.assertEqual(result["budget_status"], "fits")
        self.assertEqual(result["plan"]["outbound_minutes"], 10)
        self.assertEqual(result["plan"]["return_minutes"], 10)
        self.assertEqual(result["plan"]["total_minutes"], 45)
        self.assertEqual(result["plan"]["visit_minutes"], 20)
        self.assertTrue(result["plan"]["returns_to_origin"])
        calls = [body["params"] for _, body, _ in self.provider.requests if body["method"] == "tools/call"]
        self.assertEqual([body["name"] for body in calls], ["search_places", "compute_routes", "compute_routes"])
        self.assertEqual(calls[0]["arguments"], expected_parameters)
        self.assertEqual(calls[1]["arguments"]["destination"], {"placeId": "mock-place"})
        self.assertEqual(calls[2]["arguments"]["origin"], {"placeId": "mock-place"})
        self.assertEqual(calls[1]["arguments"]["origin"], calls[2]["arguments"]["destination"])
        self.assertEqual(calls[1]["arguments"]["origin"], {"address": "MEGA Silk Way, Astana, Kazakhstan"})
        self.assertNotIn("EXPO", calls[1]["arguments"]["origin"]["address"])
        self.assertIn("travelmode=walking", result["directions_url"])
        self.assertIn("waypoint_place_ids=mock-place", result["directions_url"])
        self.assertNotIn("DRIVE", result["directions_url"])
        self.assertEqual(self.calls[0][0], result["grounding"])
        self.assertEqual(self.budget.status()["used"], 6)
        self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_valid_empty_google_search_is_an_honest_summary_without_routes_or_model(self):
        self.provider.search = {"summary": "По этому тестовому запросу места не найдены.", "places": []}
        result = self.service.recommend(self.consent())
        self.assertEqual(result["mode"], "google-grounding-summary")
        self.assertEqual(result["grounding"], {"summary": self.provider.search["summary"], "sources": []})
        self.assertIsNone(result["answer"])
        self.assertIsNone(result["selected_place_id"])
        self.assertIsNone(result["directions_url"])
        self.assertEqual(result["budget_status"], "unverified")
        self.assertFalse(result["plan"]["verified"])
        self.assertEqual(self.budget.status()["used"], 4)
        self.assertEqual(self.calls, [])

    def test_live_far_only_central_park_is_not_selected_routed_narrated_or_cached(self):
        class Cache:
            def record_grounded(self, *args):
                raise AssertionError("Far result must never enter accepted-output archive")
        self.service.cache = Cache()
        self.provider.search["places"][0]["location"] = {"latitude": 51.1568518, "longitude": 71.41919}
        self.provider.search["summary"] = "Дальний Центральный парк Астаны упомянут источником [0]."
        original = deepcopy(self.provider.search)
        result = self.service.recommend(self.consent())
        self.assertIsNone(result["selected_place_id"])
        self.assertIsNone(result["directions_url"])
        self.assertIsNone(result["answer"])
        self.assertEqual(result["mode"], "google-grounding-summary")
        self.assertEqual(result["grounding"]["summary"], original["summary"])
        self.assertEqual(len(result["grounding"]["sources"]), 1)
        self.assertEqual(result["grounding"]["sources"][0]["location"], original["places"][0]["location"])
        self.assertEqual(result["diagnostics"]["llm_status"], "no_eligible_nearby_source")
        self.assertEqual(result["diagnostics"]["cache_status"], "not_cached_fallback")
        self.assertEqual(result["diagnostics"]["nearby_radius_meters"], 1050)
        self.assertEqual([body["params"]["name"] for _, body, _ in self.provider.requests if body["method"] == "tools/call"], ["search_places"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.budget.status()["used"], 4)

    def test_live_unfit_112_plus_112_and_unverified_routes_never_call_qwen(self):
        for duration in ("6720s", "not-a-duration"):
            with self.subTest(duration=duration):
                self.provider.route_duration = duration
                before_calls = len(self.calls)
                result = self.service.recommend(self.consent())
                self.assertIsNotNone(result["selected_place_id"])
                self.assertIsNone(result["answer"])
                self.assertEqual(result["mode"], "google-grounding-summary")
                self.assertEqual(result["diagnostics"]["llm_status"], "route_not_budget_verified")
                self.assertEqual(result["diagnostics"]["cache_status"], "not_cached_fallback")
                self.assertEqual(len(self.calls), before_calls)
                self.assertEqual(result["budget_status"], "does_not_fit" if duration == "6720s" else "unverified")
                if duration == "6720s":
                    self.assertEqual((result["plan"]["outbound_minutes"], result["plan"]["return_minutes"], result["plan"]["total_minutes"]), (112, 112, 229))

    def test_budget_radius_is_bounded_and_all_search_parameters_are_token_bound(self):
        for minutes, radius in ((10, 100), (45, 1050), (120, 2000), (480, 2000)):
            preview = self.service.preview({"query": "хочу прогулку", "origin_id": "mega", "minutes": minutes})
            self.assertEqual(preview["outbound_search_parameters"]["locationBias"]["circle"]["radiusMeters"], radius)
        payload = self.consent()
        with self.assertRaises(GoogleError):
            self.service.recommend({**payload, "minutes": 120})
        # Time/context are frozen in server RAM at preview; no second resolver
        # call can shift night/day while the user submits the same message.
        with patch.object(self.service, "resolver", side_effect=AssertionError("must stay frozen")):
            self.service.recommend(payload)

    def test_three_budgets_share_one_search_have_distinct_plans_and_one_model(self):
        base = deepcopy(self.provider.search["places"][0])
        sources = []
        for index, latitude in enumerate((51.093, 51.098, 51.103)):
            source = deepcopy(base)
            identifier = "mock-place" if index == 0 else "mock-place-" + str(index)
            source.update(id=identifier, place="places/" + identifier,
                          location={"latitude": latitude, "longitude": 71.4074})
            source["googleMapsLinks"] = {"placeUrl": "https://www.google.com/maps/place/?q=place_id:" + identifier}
            sources.append(source)
        self.provider.search = {"summary": "Три целых источника [0], [1], [2].", "places": sources}
        result = self.service.recommend(self.consent({"query": "Хочу пешком посмотреть необычное возле Mega"}))
        options = result["alternatives"]
        self.assertEqual([item["budget_minutes"] for item in options], [60, 120, 240])
        self.assertEqual(len({item["selected_place_id"] for item in options}), 3)
        self.assertEqual([item["plan"]["visit_minutes"] for item in options], [20, 40, 80])
        self.assertTrue(all(item["plan"]["returns_to_origin"] and item["budget_status"] == "fits" for item in options))
        self.assertTrue(all(item["grounding"] == result["grounding"] for item in options))
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(options[0]["answer"])
        self.assertTrue(all(item["answer"] is None for item in options[1:]))
        names = [body["params"]["name"] for _, body, _ in self.provider.requests if body["method"] == "tools/call"]
        self.assertEqual(names, ["search_places"] + ["compute_routes"] * 6)
        self.assertEqual(self.budget.status()["used"], 10)

    def test_sensitive_geolocation_routes_without_disk_prompt_or_url_start(self):
        class Cache:
            def record_grounded(self, *args):
                raise AssertionError("private-origin output must stay in RAM")
            def record_places(self, *args):
                raise AssertionError("private-origin details must stay in RAM")
        self.service.cache = Cache()
        for tool in self.provider.tools:
            if tool["name"] == "compute_routes":
                for name in ("origin", "destination"):
                    tool["inputSchema"]["properties"][name]["properties"]["latLng"] = {"type": "object"}
        form = {"query": "Хочу короткую прогулку на 45 минут", "geolocation": {"lat": 51.0892, "lon": 71.4074}}
        preview = self.service.preview(form)
        decoded = strict_json(__import__("base64").urlsafe_b64decode(preview["preview_token"].split(".")[0] + "=="))
        self.assertNotIn("51.0892", json.dumps(decoded))
        self.assertEqual(self.provider.requests, [])
        result = self.service.recommend({**form, "consent": True, "preview_token": preview["preview_token"]})
        self.assertEqual(result["diagnostics"]["cache_status"], "disabled_sensitive_origin")
        self.assertIsNone(result["directions_url"])
        self.assertTrue(result["resolved_context"]["sensitive_origin"])
        self.assertNotIn("origin", self.calls[0][2])
        self.assertNotIn("51.0892", json.dumps(self.calls[0][2]))
        calls = [body["params"] for _, body, _ in self.provider.requests if body["method"] == "tools/call"]
        self.assertEqual(calls[1]["arguments"]["origin"], {"latLng": {"latitude": 51.0892, "longitude": 71.4074}})
        details = self.service.details({"selection_token": result["selection_token"], "place_id": "mock-place", "profile": "hours"})
        self.assertEqual(details["diagnostics"]["cache_status"], "disabled_sensitive_origin")
        self.provider.search["summary"] = "Старт 51.0892, 71.4074, источник [0]."
        count = len(self.calls)
        result = self.service.recommend(self.consent(form))
        self.assertEqual(len(self.calls), count)
        self.assertIsNone(result["answer"])
        self.assertEqual(result["grounding"]["summary"], self.provider.search["summary"])
        self.assertEqual(result["diagnostics"]["llm_status"], "private_origin_in_provider_context")

    def test_cafe_instruction_and_whole_output_guard_follow_actual_intent(self):
        for text in ("Сделайте паузу в тихой атмосфере.", "Проведите время в тихом кафе.", "Здесь тишина."):
            with self.subTest(text=text):
                self.assertEqual(protected_claim_rejection(text), "unsupported_protected_quiet")
        self.assertIsNone(protected_claim_rejection("Тишина не подтверждена."))
        from app.google_grounded import intent_action_instruction, intent_action_rejection
        instruction = intent_action_instruction({"categories": ["cafe"]})
        self.assertIn("пауза", instruction.replace("паузу", "пауза"))
        self.assertNotIn("Предложи внешний осмотр", instruction)
        wrong = {"text": "Кафе предлагает кофе для короткого перерыва. Осмотрите фасад здания."}
        self.assertEqual(intent_action_rejection(wrong, {"categories": ["cafe"]}), "action_does_not_match_cafe_intent")
        self.assertIsNone(intent_action_rejection({"text": "Сделайте короткую паузу на кофе. Условия посещения неизвестны."}, {"categories": ["cafe"]}))
        self.assertIsNone(intent_action_rejection(wrong, {"categories": ["architecture"]}))
        self.service.narrator = lambda *args: ({**good_answer(), **wrong}, "mocked")
        result = self.service.recommend(self.consent({"query": "Хочу кафе с кофе у Mega на 60 минут"}))
        self.assertIsNone(result["answer"])
        self.assertEqual(result["diagnostics"]["llm_status"], "action_does_not_match_cafe_intent")
        self.assertEqual(result["grounding"]["summary"], self.provider.search["summary"])

    def test_private_address_echo_with_comma_is_rejected_unchanged(self):
        context = {"sensitive_origin": True, "origin_source": "query_catalog",
                   "origin": {"lat": 51.080123, "lon": 71.400123, "label": "Тестовая улица, 12"}}
        self.assertTrue(_contains_private_origin("Старт: Тестовая улица, 12.", context))
        self.assertTrue(_contains_private_origin("Старт 51.0801 и 71.4001.", context))
        self.assertFalse(_contains_private_origin("Другой публичный источник.", context))

    def test_later_route_failure_never_reuses_previous_success(self):
        original = self.client.compute_route
        count = [0]
        def route(*args):
            count[0] += 1
            if count[0] in (3, 4):
                raise GoogleError("google_timeout", "safe")
            return original(*args)
        self.client.compute_route = route
        result = self.service.recommend(self.consent({"query": "Хочу прогулку возле Mega"}))
        options = result["alternatives"]
        self.assertEqual(options[0]["budget_status"], "fits")
        self.assertEqual(options[1]["budget_status"], "unverified")
        self.assertIsNone(options[1]["plan"]["total_minutes"])
        self.assertEqual(len(options[1]["diagnostics"]["route_errors"]), 2)
        self.assertEqual(options[2]["budget_status"], "fits")
        self.assertEqual(len(self.calls), 1)

    def test_closest_uses_metadata_not_first_or_summary_names(self):
        prototype = deepcopy(self.provider.search["places"][0])
        self.provider.search["places"][0]["location"] = {"latitude": 52, "longitude": 72}
        origin = deepcopy(prototype)
        origin.update(id="origin", place="places/origin")
        origin["location"] = {"latitude": 51.0892, "longitude": 71.4074}
        origin["googleMapsLinks"]["placeUrl"] = "https://www.google.com/maps/place/?q=place_id:origin"
        nearby = deepcopy(prototype)
        nearby.update(id="nearby", place="places/nearby")
        nearby["location"] = {"latitude": 51.0937, "longitude": 71.4074}
        nearby["googleMapsLinks"]["placeUrl"] = "https://www.google.com/maps/place/?q=place_id:nearby"
        self.provider.search["places"].extend([origin, nearby])
        self.provider.search["summary"] = "Дальний источник [0], сам старт [1] и другая близкая остановка [2]."
        self.service.narrator = lambda *args: (None, "not_called_live")
        for query, expected, minimum in (
            (FORM["query"], "nearby", 100),
            ("хочу архитектуру", "origin", 0),
            ("хочу кофе и прогулку", "origin", 0),
        ):
            with self.subTest(query=query):
                form = {"query": query, "origin_id": "mega", "minutes": 45}
                result = self.service.recommend(self.consent(form))
                self.assertEqual(result["selected_place_id"], expected)
                self.assertEqual(result["diagnostics"]["minimum_stop_distance_meters"], minimum)
                self.assertEqual(len(result["grounding"]["sources"]), 3)
                self.assertEqual(result["grounding"]["summary"], self.provider.search["summary"])
        self.provider.search["places"] = [origin]
        self.provider.search["summary"] = "Только сам старт [0]."
        before_model, before_http = len(self.calls), len(self.provider.requests)
        result = self.service.recommend(self.consent())
        self.assertIsNone(result["selected_place_id"])
        self.assertIsNone(result["answer"])
        self.assertEqual(len(self.calls), before_model)
        self.assertEqual(len(self.provider.requests) - before_http, 1)  # Search only; no legs.


    def test_unknown_invalid_and_long_routes_never_claim_fit(self):
        attribution = {"title": "Google Maps", "url": "https://www.google.com/maps"}
        route = lambda duration: {"routes": [{"duration": duration, "distanceMeters": 1000, "attribution": attribution}]}
        for outgoing, incoming in (({}, route("60s")), (route("600s"), {"routes": []}), (route("NaNs"), route("600s"))):
            plan, status = make_plan(45, outgoing, incoming)
            self.assertEqual(status, "unverified")
            self.assertFalse(plan["verified"])
            self.assertIsNone(plan["total_minutes"])
            self.assertNotIn("returned both", plan["verification_scope"])
        plan, status = make_plan(45, route("1200s"), route("2400s"))
        self.assertEqual(status, "does_not_fit")
        self.assertEqual(plan["outbound_minutes"], 20)
        self.assertEqual(plan["return_minutes"], 40)
        self.assertGreater(plan["total_minutes"], 45)

    def test_failed_route_keeps_source_and_explicit_summary_fallback(self):
        self.service.narrator = lambda *args: (None, "guard_rejected")
        self.client.compute_route = lambda *args: (_ for _ in ()).throw(GoogleError("google_unavailable", "safe"))
        result = self.service.recommend(self.consent())
        self.assertEqual(result["mode"], "google-grounding-summary")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["grounding"]["summary"], self.provider.search["summary"])
        self.assertEqual(result["budget_status"], "unverified")
        self.assertEqual(len(result["diagnostics"]["route_errors"]), 2)

    def test_source_and_details_selection_validation_no_guessed_osm_mapping(self):
        result = self.service.recommend(self.consent())
        token = result["selection_token"]
        for payload in ({"selection_token": token, "place_id": "osm:node/1", "profile": "hours"},
                        {"selection_token": token, "place_id": "other", "profile": "reviews"},
                        {"selection_token": self.consent()["preview_token"], "place_id": "mock-place", "profile": "hours"}):
            before = self.budget.status()["used"]
            with self.assertRaises(GoogleError):
                self.service.details(payload)
            self.assertEqual(self.budget.status()["used"], before)
        self.service.details({"selection_token": token, "place_id": "mock-place", "profile": "hours"})
        self.service.details({"selection_token": token, "place_id": "mock-place", "profile": "reviews"})
        self.assertEqual(self.budget.status()["used"], 8)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_sources_are_rejected_without_parsing_summary_into_poi(self):
        original = deepcopy(self.provider.search)
        cases = []
        for mutation in (lambda data: data["places"][0].update(id="other"),
                         lambda data: data["places"][0]["googleMapsLinks"].update(placeUrl="https://evil.invalid/"),
                         lambda data: data.update(summary="source missing [99]"),
                         lambda data: data["places"][0].update(location={"latitude": float("nan"), "longitude": 1})):
            item = deepcopy(original)
            mutation(item)
            cases.append(item)
        for data in cases:
            with self.assertRaises(GoogleError):
                validate_grounding(data)

    def test_llm_payload_unchanged_full_context_local_only_cache_disabled(self):
        grounding = validate_grounding(self.provider.search)
        selected = "mock-place"
        context = {"origin_id": "mega", "time_of_day": "day"}
        plan = {"verified": True, "visit_minutes": 20, "total_minutes": 45, "budget_minutes": 45,
                "route_attributions": [{"title": "PRIVATE_ROUTE_MARKER", "url": "https://www.google.com/maps?origin=51.0892,71.4074"}]}
        requests = []
        def model(request, timeout):
            requests.append((request, timeout))
            return Response({"choices": [{"message": {"content": json.dumps(good_answer(), ensure_ascii=False)}}]})
        answer, status = narrate(grounding, selected, context, {"categories": ["park"], "preferences": []}, plan, "fits", self.config, http_open=model)
        self.assertEqual(answer, good_answer())
        payload = strict_json(requests[0][0].data)
        self.assertIs(payload["cache_prompt"], False)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 450)
        self.assertEqual(requests[0][1], 120)
        system = payload["messages"][0]["content"]
        self.assertIn("не более трёх коротких предложений", system)
        self.assertIn("до 360 символов", system)
        sent = strict_json(payload["messages"][1]["content"])
        self.assertEqual(sent["grounding_lite_summary"], grounding["summary"])
        self.assertEqual(len(sent["all_google_source_previews"]), len(grounding["sources"]))
        for preview, source in zip(sent["all_google_source_previews"], grounding["sources"]):
            self.assertEqual(set(preview), {"id", "source_id", "title", "url", "attribution"})
            for field in preview:
                self.assertEqual(preview[field], source.get(field))
        self.assertNotIn("route_attributions", sent["plan"])
        self.assertNotIn("PRIVATE_ROUTE_MARKER", json.dumps(sent))
        self.assertEqual(sent["selected_source_id"], "g0")
        self.assertEqual(set(sent), {"grounding_lite_summary", "all_google_source_previews", "selected_place_id", "selected_source_id", "public_origin_id", "intent", "time_of_day", "plan", "budget_status"})
        self.assertEqual(requests[0][0].full_url, "http://127.0.0.1:8081/v1/chat/completions")
        self.assertNotIn("api-key", str(dict(requests[0][0].headers)).lower())
        for config in (None, LLMConfig("https://cloud.invalid/v1", "x", "secret"), LLMConfig("http://127.0.0.1:8081/v1", "x", "secret")):
            self.assertIsNone(narrate(grounding, selected, context, {}, plan, "fits", config, http_open=model)[0])
        self.assertIsNone(narrate(grounding, selected, context, {}, plan, "does_not_fit", self.config, http_open=model)[0])
        self.assertEqual(len(requests), 1)

    def test_five_long_link_sources_keep_full_summary_all_previews_and_ui_unchanged(self):
        places = []
        for index in range(5):
            identifier = "mock-source-" + str(index)
            url = "https://www.google.com/maps/place/?q=place_id:" + identifier
            links = {"placeUrl": url}
            for action in ("photosUrl", "writeAReviewUrl", "reviewsUrl", "directionsUrl"):
                links[action] = "https://www.google.com/maps/" + action + "?opaque=" + ("encoded-metadata-" * 100) + str(index)
            places.append({"id": identifier, "place": "places/" + identifier,
                           "location": {"latitude": 51.09 + index / 10000, "longitude": 71.41},
                           "googleMapsLinks": links,
                           "attribution": {"title": "Фиктивный источник " + str(index), "url": url}})
        summary = ("Полный неизменённый тестовый текст. " * 40) + " Источники [0], [1], [2], [3], [4]."
        grounding = validate_grounding({"summary": summary, "places": places})
        original = deepcopy(grounding)
        captured = []
        def model(request, timeout):
            captured.append((strict_json(request.data), timeout))
            answer = {"text": GOOD_TEXT, "selected_place_ids": ["mock-source-0"], "source_ids": ["g0"]}
            return Response({"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})
        plan = {"verified": True, "visit_minutes": 20, "total_minutes": 45, "budget_minutes": 45}
        with patch("app.google_grounded.open_local_model", side_effect=AssertionError("No actual model")):
            answer, status = narrate(grounding, "mock-source-0", {"origin_id": "mega", "time_of_day": "day"},
                                    {"categories": ["walk"], "preferences": []}, plan, "fits", self.config, http_open=model)
        self.assertIsNotNone(answer)
        self.assertEqual(len(captured), 1)
        payload, timeout = captured[0]
        self.assertEqual(timeout, 120)
        self.assertIs(payload["cache_prompt"], False)
        self.assertEqual(payload["max_tokens"], 450)
        sent = strict_json(payload["messages"][1]["content"])
        self.assertEqual(sent["grounding_lite_summary"], summary)
        previews = sent["all_google_source_previews"]
        self.assertEqual(len(previews), 5)
        self.assertEqual([item["id"] for item in previews], [item["id"] for item in original["sources"]])
        for preview, source in zip(previews, original["sources"]):
            self.assertEqual(set(preview), {"id", "source_id", "title", "url", "attribution"})
            for field in preview:
                self.assertEqual(preview[field], source.get(field))
        before = {**sent, "all_google_source_previews": original["sources"]}
        self.assertLess(len(json_bytes(sent)), len(json_bytes(before)) * 0.4)
        self.assertEqual(grounding, original)  # UI and archive still have every original URL/location.
        self.assertTrue(all("photosUrl" in source["googleMapsLinks"] for source in grounding["sources"]))
        self.assertEqual(self.provider.requests, [])

    def test_selected_nonfirst_source_is_bound_in_schema_context_and_existing_guard(self):
        places = []
        for index in range(5):
            place = deepcopy(self.provider.search["places"][0])
            place["id"] = "mock-source-" + str(index)
            place["place"] = "places/" + place["id"]
            place["googleMapsLinks"]["placeUrl"] = "https://www.google.com/maps/place/?q=place_id:" + place["id"]
            place["attribution"]["title"] = "Фиктивный источник " + str(index)
            places.append(place)
        summary = "Полный тестовый summary: источники [0], [1], [2], [3], [4]."
        grounding = validate_grounding({"summary": summary, "places": places})
        original = deepcopy(grounding)
        captured = []
        plan = {"verified": True, "visit_minutes": 20, "total_minutes": 45, "budget_minutes": 45}
        context = {"origin_id": "mega", "time_of_day": "day"}
        with patch("app.google_grounded.open_local_model", side_effect=AssertionError("No actual model")):
            for cited in ("g0", "g2"):
                expected = {"text": GOOD_TEXT.replace("[0]", "[2]"), "selected_place_ids": ["mock-source-2"], "source_ids": [cited]}
                def model(request, timeout):
                    captured.append((strict_json(request.data), timeout))
                    return Response({"choices": [{"message": {"content": json.dumps(expected, ensure_ascii=False)}}]})
                answer, status = narrate(grounding, "mock-source-2", context, {"categories": ["walk"], "preferences": []},
                                        plan, "fits", self.config, http_open=model)
                with self.subTest(cited=cited):
                    if cited == "g0":
                        self.assertIsNone(answer)
                        self.assertEqual(status, "selected_source_missing")
                    else:
                        self.assertEqual(answer, expected)
                        self.assertEqual(status, "validated_structure_sources_and_known_claims")
                    payload, timeout = captured[-1]
                    schema = payload["response_format"]["json_schema"]["schema"]["properties"]["source_ids"]
                    self.assertEqual(schema, {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "string", "enum": ["g2"]}})
                    sent = strict_json(payload["messages"][1]["content"])
                    self.assertEqual(sent["selected_source_id"], "g2")
                    self.assertEqual(sent["selected_place_id"], "mock-source-2")
                    self.assertEqual(sent["grounding_lite_summary"], summary)
                    self.assertEqual([source["source_id"] for source in sent["all_google_source_previews"]], ["g0", "g1", "g2", "g3", "g4"])
                    self.assertIn("source_ids должен содержать ровно selected_source_id", payload["messages"][0]["content"])
                    self.assertEqual(payload["max_tokens"], 450)
                    self.assertIs(payload["cache_prompt"], False)
                    self.assertEqual(timeout, 120)
            for missing in ("not-in-sources", None):
                self.assertEqual(narrate(grounding, missing, context, {}, plan, "fits", self.config, http_open=model),
                                 (None, "selected_source_missing"))
            ambiguous = deepcopy(grounding)
            ambiguous["sources"].append(deepcopy(grounding["sources"][2]))
            self.assertEqual(narrate(ambiguous, "mock-source-2", context, {}, plan, "fits", self.config, http_open=model),
                             (None, "selected_source_missing"))
        self.assertEqual(len(captured), 2)  # Missing/ambiguous associations never reach the model.
        self.assertEqual(grounding, original)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)

    def test_incomplete_model_json_keeps_summary_fallback_without_repair_or_archive(self):
        class Cache:
            def record_grounded(self, *args):
                raise AssertionError("Incomplete model output must not be archived")
        self.service.cache = Cache()
        model_requests = []
        incomplete = json.dumps(good_answer(), ensure_ascii=False)[:-1]
        def model(request, timeout):
            model_requests.append((request, timeout))
            return Response({"choices": [{"finish_reason": "length", "message": {"content": incomplete}}]})
        self.service.narrator = lambda *args: narrate(*args, http_open=model)
        with patch("app.google_grounded.open_local_model", side_effect=AssertionError("No actual model")):
            result = self.service.recommend(self.consent())
        self.assertEqual(result["mode"], "google-grounding-summary")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["diagnostics"]["llm_status"], "local_model_unavailable_or_invalid")
        self.assertEqual(result["diagnostics"]["cache_status"], "not_cached_fallback")
        self.assertEqual(result["grounding"], validate_grounding(self.provider.search))
        self.assertEqual(result["budget_status"], "fits")
        self.assertEqual(len(model_requests), 1)  # No repair or retry after truncation.
        self.assertEqual(strict_json(model_requests[0][0].data)["max_tokens"], 450)
        self.assertEqual(model_requests[0][1], 120)
        self.assertEqual(self.budget.status()["used"], 6)  # Mock MCP only.

    def test_whole_answer_guard_rejects_source_numeric_transport_and_protected_claims(self):
        grounding = validate_grounding(self.provider.search)
        plan = {"verified": False, "visit_minutes": None, "total_minutes": None, "budget_minutes": 45}
        for text in ("Здесь есть розетки, бесплатный Wi-Fi и безопасная детская площадка.",
                     "Кафе открыто сейчас, там тихо и доступно всем.",
                     "Кафе открыто сейчас, но уточните часы перед выходом.",
                     "Это тихое кафе с розетками, проверьте доступ.",
                     "В кафе нет розеток, но проверьте условия.",
                     "Кафе не работает сейчас, выберите другое.",
                     "Это идеальное место с доступной площадкой для всех.",
                     "Можно доехать на bike и увидеть парк из источника.",
                     "Здесь прогулка займёт 9999 минут и точно укладывается в бюджет.",
                     "Вы успеете вернуться, маршрут укладывается в бюджет."):
            answer = good_answer()
            answer["text"] = text
            self.assertIsNotNone(_answer_rejection(answer, grounding, "mock-place", plan), text)
        for key, value in (("selected_place_ids", ["invented"]), ("source_ids", ["g99"]), ("text", GOOD_TEXT + " [99]")):
            answer = good_answer()
            answer[key] = value
            self.assertIsNotNone(_answer_rejection(answer, grounding, "mock-place", plan))
        self.assertIsNone(_answer_rejection(good_answer(), grounding, "mock-place", plan))
        for text in ("Розетки неизвестны; проверьте условия.", "Тишина и безопасность не подтверждены.", "Перед выходом проверьте, открыт ли вход."):
            self.assertIsNone(protected_claim_rejection(text), text)

    def test_cache_worker_failure_is_visible_sanitized_and_does_not_call_provider(self):
        class Cache:
            cleanup_error = "PRIVATE_RUNTIME_PATH_OR_ERROR"
            def status(self):
                return {"enabled": True, "error": None}
        cache = Cache()
        self.service.cache = cache
        status = self.service.status()
        self.assertFalse(status["cache"]["available"])
        self.assertEqual(status["cache"]["error"], "google_cache_unavailable")
        self.assertEqual(status["cache"]["cleanup_error"], "google_cache_unavailable")
        self.assertNotIn("PRIVATE_RUNTIME", json.dumps(status))
        self.assertEqual(self.provider.requests, [])
        cache.cleanup_error = None
        self.assertTrue(self.service.status()["cache"]["available"])

    def test_only_whole_accepted_answer_and_valid_details_reach_optional_cache(self):
        class Cache:
            def __init__(self):
                self.values = []
            def record_grounded(self, answer, sources):
                self.values.append(("grounded", deepcopy(answer), deepcopy(sources)))
            def record_places(self, data):
                self.values.append(("places", deepcopy(data)))
        cache = Cache()
        self.service.cache = cache
        result = self.service.recommend(self.consent())
        self.assertEqual(result["diagnostics"]["cache_status"], "stored")
        self.assertEqual(cache.values[0], ("grounded", result["answer"], result["grounding"]["sources"]))
        self.service.details({"selection_token": result["selection_token"], "place_id": "mock-place", "profile": "hours"})
        self.assertEqual(cache.values[1][0], "places")
        self.service.narrator = lambda *args: (None, "rejected")
        result = self.service.recommend(self.consent())
        self.assertEqual(result["diagnostics"]["cache_status"], "not_cached_fallback")
        self.assertEqual(len(cache.values), 2)
        cache.record_grounded = lambda *args: (_ for _ in ()).throw(OSError("PRIVATE_PATH"))
        self.service.narrator = lambda *args: (good_answer(), "mocked")
        result = self.service.recommend(self.consent())
        self.assertEqual(result["mode"], "google-grounded-llm")
        self.assertEqual(result["diagnostics"]["cache_status"], "unavailable")
        self.assertNotIn("PRIVATE_PATH", json.dumps(result))

    def test_model_transport_ignores_ambient_proxy_and_refuses_redirects(self):
        from app.google_grounded import open_local_model
        with patch("app.google_grounded.build_opener") as builder:
            builder.return_value.open.return_value = Response({})
            open_local_model(object(), timeout=3)
            handlers = builder.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsNone(handlers[1].redirect_request(None, None, 302, "x", {}, "https://else.invalid"))


class GoogleRoutingTests(unittest.TestCase):
    setUp = lab_tests.GoogleLabTests.setUp
    write_fixture = lab_tests.GoogleLabTests.write_fixture

    def test_default_disabled_routes_and_no_startup_network(self):
        with patch("socket.create_connection", side_effect=AssertionError("No provider")):
            with lab.create_server(port=0, root=self.root) as server:
                self.assertFalse(json.loads(server.health_json)["google"]["enabled"])
        with running_server(self.root) as (server, _):
            status, headers, body = request(server, "/api/google/status")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["enabled"])
            status, headers, body = request(server, "/api/google/preview", "POST", json_bytes(FORM), {"Content-Type": "application/json"})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["code"], "google_disabled")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_server_owns_only_local_optional_cache_worker_lifecycle(self):
        class Cache:
            def __init__(self):
                self.events = []
            def start_cleanup(self):
                self.events.append("start")
            def close(self):
                self.events.append("close")
        cache = Cache()
        budget_path = self.root / "mock-ledger.json"
        GoogleBudget.initialize(budget_path)
        provider = Provider()
        google = GoogleGrounded(GoogleClient("MOCK_KEY", GoogleBudget(budget_path), http_open=provider),
                                lab.resolve_request, cache=cache)
        with lab.create_server(port=0, root=self.root, google=google):
            self.assertEqual(cache.events, ["start"])
            self.assertTrue(google.status()["cache"]["available"])
        self.assertEqual(cache.events, ["start", "close"])
        self.assertEqual(provider.requests, [])

    def test_injected_google_routes_strict_http_no_osm_or_provider_mix(self):
        budget_path = self.root / "mock-ledger.json"
        GoogleBudget.initialize(budget_path)
        provider = Provider()
        client = GoogleClient("MOCK_KEY", GoogleBudget(budget_path), http_open=provider)
        google = GoogleGrounded(client, lab.resolve_request, narrator=lambda *args: (None, "mocked_fallback"))
        with running_server(self.root, google=google) as (server, _):
            def post(path, value, headers=None):
                status, response_headers, raw = request(server, path, "POST", json_bytes(value), headers or {"Content-Type": "application/json"})
                return status, response_headers, json.loads(raw)
            status, _, preview = post("/api/google/preview", FORM)
            self.assertEqual(status, 200)
            self.assertEqual(provider.requests, [])
            status, _, result = post("/api/google/recommend", {**FORM, "consent": True, "preview_token": preview["preview_token"]})
            self.assertEqual(status, 200)
            self.assertEqual(result["mode"], "google-grounding-summary")
            self.assertNotIn("dataset", result)
            self.assertNotIn("recommendations", result)
            health = json.loads(server.health_json)
            self.assertEqual(health["google_http_attempts_pilot"], 6)
            self.assertTrue(health["backend_outbound_calls"])
            self.assertFalse(health["local_llm_outbound_calls"])
            self.assertIn("shared pilot ledger", health["backend_outbound_calls_scope"])
            before = len(provider.requests)
            status, _, result = post("/api/google/details", {"selection_token": "invalid", "place_id": "x", "profile": "hours"})
            self.assertEqual(status, 400)
            self.assertEqual(len(provider.requests), before)
            status, _, result = post("/api/google/preview", FORM, {"Content-Type": "application/json", "Origin": "https://evil.invalid"})
            self.assertEqual(status, 403)
            status, _, result = post("/api/recommend", FORM)
            self.assertEqual(status, 200)
            self.assertIn("recommendations", result)
            self.assertEqual(len(provider.requests), before)


if __name__ == "__main__":
    unittest.main()
