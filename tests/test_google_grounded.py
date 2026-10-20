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
from app.google_grounded import GoogleGrounded, TokenSigner, validate_grounding, make_plan, narrate, _answer_rejection, protected_claim_rejection
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
        self.service = GoogleGrounded(self.client, lab.resolve_request, llm_config=self.config, narrator=narrator)

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
        self.assertIn("45 минут", preview["outbound_search_text"])
        self.assertIn("Время суток: day", preview["outbound_search_text"])
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
                    for interest in ("достопримечательности", "архитектура", "площади", "бульвары"):
                        self.assertIn(interest, preview["outbound_search_text"])
                    self.assertNotIn("парки", preview["outbound_search_text"])
                if categories == ["park"]:
                    self.assertIn("парки и сады", preview["outbound_search_text"])
                    self.assertNotIn("достопримечательности", preview["outbound_search_text"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)

    def test_unknown_context_custom_gps_and_unsupported_intent_have_no_network(self):
        cases = ({"query": "кофе"}, {"query": "хочу бассейн", "origin_id": "mega", "minutes": 45},
                 {"query": "прогулка", "origin": {"lat": 51.0892, "lon": 71.4074, "label": "PRIVATE_HOME"}, "minutes": 45})
        for form in cases:
            with self.subTest(form=form), self.assertRaises((GoogleError, lab.ContextError)):
                self.service.preview(form)
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
        from app.google_grounded import prepare
        with patch("app.google_grounded.prepare", wraps=prepare) as wrapped:
            original = prepare(FORM, lab.resolve_request)
            changed = list(original)
            changed[3] = "0" * 64
            wrapped.return_value = tuple(changed)
            with self.assertRaises(GoogleError):
                self.service.recommend(payload)
        self.assertEqual(self.provider.requests, [])

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
        plan = {"verified": True, "visit_minutes": 20, "total_minutes": 45, "budget_minutes": 45}
        requests = []
        def model(request, timeout):
            requests.append((request, timeout))
            return Response({"choices": [{"message": {"content": json.dumps(good_answer(), ensure_ascii=False)}}]})
        answer, status = narrate(grounding, selected, context, {"categories": ["park"], "preferences": []}, plan, "fits", self.config, http_open=model)
        self.assertEqual(answer, good_answer())
        payload = strict_json(requests[0][0].data)
        self.assertIs(payload["cache_prompt"], False)
        self.assertEqual(payload["temperature"], 0)
        sent = strict_json(payload["messages"][1]["content"])
        self.assertEqual(sent["grounding_lite_summary"], grounding["summary"])
        self.assertEqual(sent["all_google_source_previews"], grounding["sources"])
        self.assertEqual(set(sent), {"grounding_lite_summary", "all_google_source_previews", "selected_place_id", "public_origin_id", "intent", "time_of_day", "plan", "budget_status"})
        self.assertEqual(requests[0][0].full_url, "http://127.0.0.1:8081/v1/chat/completions")
        self.assertNotIn("api-key", str(dict(requests[0][0].headers)).lower())
        for config in (None, LLMConfig("https://cloud.invalid/v1", "x", "secret"), LLMConfig("http://127.0.0.1:8081/v1", "x", "secret")):
            self.assertIsNone(narrate(grounding, selected, context, {}, plan, "fits", config, http_open=model)[0])
        self.assertIsNone(narrate(grounding, selected, context, {}, plan, "does_not_fit", self.config, http_open=model)[0])
        self.assertEqual(len(requests), 1)

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
