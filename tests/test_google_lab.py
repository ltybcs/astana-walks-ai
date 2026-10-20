"""Isolated lab regressions; no model, Google, 2GIS or Overpass requests."""
from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
import hashlib
from http.client import HTTPConnection, HTTPResponse
from email.message import Message
import io
import json
import socket
import time
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from app import google_lab as lab


@contextmanager
def running_server(root, **kwargs):
    with lab.create_server(port=0, root=root, **kwargs) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server, thread
        finally:
            server.shutdown()
            thread.join(timeout=3)
    if thread.is_alive():
        raise AssertionError("Owned lab server thread leaked")


def request(server, path, method="GET", body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class GoogleLabTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw_path = self.root / "data/snapshots/raw.json"
        self.raw_path.parent.mkdir(parents=True)
        self.base_places, self.elements = [], []
        cases = [
            ("cafe", "Кафе", False, {"name": "Кафе", "access": "customers", "cuisine": "regional;coffee_shop", "operator": "Оператор из источника"}),
            ("playground", "Без названия: площадка (OSM node/2)", True, {"leisure": "playground", "access": "private"}),
            ("park", "Без названия: парк (OSM node/3)", True, {"leisure": "park", "access": "no"}),
            ("park", "Без названия: парк (OSM node/4)", True, {"leisure": "garden"}),
        ]
        for number, (category, name, generated, tags) in enumerate(cases, 1):
            self.base_places.append({"id": f"osm:node/{number}", "name": name, "category": category, "description": "Описание тестового источника.", "lat": 51.1 + number / 1000, "lon": 71.4, "source_tags": {"access": "yes"}, "name_generated": generated, "source_name": None if generated else name, "features": {"socket": "unknown", "quiet": "unknown"}, "source_url": f"https://www.openstreetmap.org/node/{number}"})
            self.elements.append({"type": "node", "id": number, "tags": tags, "timestamp": "2026-10-19T21:29:20Z"})
        self.write_fixture()
        assets = {"web/google-lab/index.html": b"<!doctype html><title>Lab fixture</title>", "web/google-lab/lab.js": b'"use strict";', "web/google-lab/lab.css": b"body{color:#000}", "web/google-lab/embeds.json": b'{"places":[]}'}
        for relative, _mime in lab.STATIC_FILES.values():
            if relative not in assets:
                assets[relative] = b"local-vendor-fixture"
        for relative, raw in assets.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        private = self.root / ".local/secret"
        private.parent.mkdir()
        private.write_bytes(b"private-not-a-route")

    def write_fixture(self):
        raw = json.dumps({"elements": self.elements}, ensure_ascii=False).encode("utf-8")
        self.raw_path.write_bytes(raw)
        document = {"dataset": {"name": "OSM test fixture", "is_demo": False, "collected_at": "2026-10-19T21:30:22+00:00", "raw_snapshot": "data/snapshots/raw.json", "raw_snapshot_sha256": hashlib.sha256(raw).hexdigest()}, "places": self.base_places}
        (self.root / "data/poi.astana.json").write_bytes(json.dumps(document, ensure_ascii=False).encode("utf-8"))

    def test_raw_access_overrides_missing_or_stale_normalized_access(self):
        before = (self.root / "data/poi.astana.json").read_bytes()
        result = lab.load_catalog(self.root)
        self.assertEqual(result["summary"], {"source_count": 4, "visible_count": 2, "excluded_restricted_count": 2, "categories": {"cafe": 1, "park": 1}, "generated_names_count": 1})
        self.assertEqual([place["id"] for place in result["places"]], ["osm:node/1", "osm:node/4"])
        cafe, unnamed = result["places"]
        self.assertEqual(cafe["access_evidence"], "customers")
        self.assertEqual(cafe["cuisine"], "regional;coffee_shop")
        self.assertEqual(cafe["operator"], "Оператор из источника")
        self.assertEqual(cafe["source_tags"]["access"], "customers")
        self.assertEqual(cafe["source"], "OSM")
        self.assertEqual(unnamed["name"], self.base_places[3]["name"])
        self.assertIsNone(unnamed["source_name"])
        self.assertEqual(unnamed["access_evidence"], "unknown")
        self.assertEqual(cafe["raw_provenance"]["snapshot_sha256"], hashlib.sha256(self.raw_path.read_bytes()).hexdigest())
        self.assertEqual((self.root / "data/poi.astana.json").read_bytes(), before)

    def test_shipped_catalog_has_1267_sources_and_excludes_the_actual_private_playground(self):
        result = lab.load_catalog()
        self.assertEqual(result["summary"]["source_count"], 1267)
        self.assertEqual(result["summary"]["visible_count"], 1266)
        self.assertEqual(result["summary"]["excluded_restricted_count"], 1)
        self.assertEqual(result["summary"]["generated_names_count"], 477)
        self.assertNotIn("osm:way/1419934935", {place["id"] for place in result["places"]})
        self.assertEqual(sum(place["access_evidence"] == "customers" for place in result["places"]), 3)
        self.assertEqual(sum(bool(place["cuisine"]) for place in result["places"]), 182)

    def test_raw_snapshot_must_be_exact_and_complete(self):
        self.raw_path.write_bytes(self.raw_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            lab.load_catalog(self.root)
        self.elements.pop()
        self.write_fixture()
        with self.assertRaisesRegex(ValueError, "no unique raw"):
            lab.load_catalog(self.root)

    def test_strict_sources_reject_overflow_and_duplicate_keys(self):
        for raw in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lab._read_json(raw)

    def test_source_traversal_cannot_read_a_file_outside_snapshot_folder(self):
        path = self.root / "data/poi.astana.json"
        document = json.loads(path.read_bytes())
        document["dataset"]["raw_snapshot"] = "data/snapshots/../../../secret"
        path.write_bytes(json.dumps(document).encode())
        with self.assertRaises(ValueError):
            lab.load_catalog(self.root)

    def test_startup_and_catalog_use_no_outbound_provider_connection(self):
        with patch("socket.socket.connect", side_effect=AssertionError("No provider calls")), patch("socket.create_connection", side_effect=AssertionError("No provider calls")), patch("urllib.request.urlopen", side_effect=AssertionError("No provider calls")):
            with lab.create_server(port=0, root=self.root) as server:
                self.assertEqual(server.catalog["summary"]["visible_count"], 2)
                self.assertEqual(json.loads(server.health_json)["provider_calls"], 0)
        self.assertFalse((self.root / ".local/2gis").exists())
        self.assertFalse((self.root / ".local/poi").exists())

    def test_get_head_security_headers_and_no_sensitive_logging(self):
        log = io.StringIO()
        with redirect_stderr(log), running_server(self.root) as (server, _thread):
            status, headers, body = request(server, "/api/catalog")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["summary"]["visible_count"], 2)
            head_status, head_headers, head_body = request(server, "/api/catalog", "HEAD")
            self.assertEqual(head_status, 200)
            self.assertEqual(head_body, b"")
            self.assertEqual(int(head_headers["Content-Length"]), len(body))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-src https://www.google.com", headers["Content-Security-Policy"])
            self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Permissions-Policy"], "geolocation=(self), camera=(), microphone=()")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            status, _, body = request(server, "/api/health")
            health = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(health["lab"] and health["free"])
            self.assertEqual(health["provider_calls"], 0)
            request(server, "/query-private-123?lat=51.12345")
        self.assertEqual(log.getvalue(), "")

    def test_only_explicit_static_routes_are_served(self):
        with running_server(self.root) as (server, _thread):
            for path, (relative, mime) in lab.STATIC_FILES.items():
                with self.subTest(path=path):
                    status, headers, raw = request(server, path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], mime)
                    self.assertEqual(raw, (self.root / relative).read_bytes())
            for path in ("/index.html", "/app/server.py", "/.git/config", "/.local/secret", "/../.local/secret", "/%2e%2e/.local/secret", "/web/google-lab/", "/vendor/leaflet/1.9.4/LICENSE", "/lab.js?raw=1", "/api/recommend", "/api/2gis/search"):
                with self.subTest(path=path):
                    status, headers, raw = request(server, path)
                    self.assertEqual(status, 404)
                    self.assertNotIn(b"private-not-a-route", raw)
                    self.assertEqual(headers["Cache-Control"], "no-store")

    def test_mutation_methods_are_disabled_and_foreign_origins_cannot_read_catalog(self):
        with running_server(self.root) as (server, _thread):
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                with self.subTest(method=method):
                    status, _, _ = request(server, "/api/2gis/search", method, body=b"not forwarded")
                    self.assertEqual(status, 404 if method == "POST" else 405)
            for headers in ({"Origin": "https://foreign.example"}, {"Host": "foreign.example"}, {"Host": "127.0.0.1:1"}):
                with self.subTest(headers=headers):
                    self.assertEqual(request(server, "/api/catalog", headers=headers)[0], 403)

    def test_early_415_and_405_drain_delayed_small_bodies_before_closing(self):
        with running_server(self.root) as (server, _thread):
            for method, path, content_type, expected in (
                ("POST", "/api/recommend", "text/plain", 415),
                ("PUT", "/api/2gis/search", "application/json", 405),
            ):
                with self.subTest(method=method), socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
                    payload = b"not forwarded"
                    headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                               f"Content-Type: {content_type}\r\nContent-Length: {len(payload)}\r\n\r\n").encode("ascii")
                    connection.sendall(headers)
                    connection.settimeout(0.04)
                    # Old behavior returned/closed before the declared body.
                    with self.assertRaises(TimeoutError):
                        connection.recv(1)
                    connection.sendall(payload)
                    connection.settimeout(2)
                    response = HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, expected)
                    self.assertEqual(response.getheader("Connection"), "close")
                    self.assertTrue(response.read())
                    response.close()

    def test_early_reject_has_a_total_deadline_when_declared_body_is_withheld(self):
        with running_server(self.root) as (server, _thread), socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
            headers = (f"PUT /api/2gis/search HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                       "Content-Length: 16384\r\n\r\n").encode("ascii")
            start = time.monotonic()
            connection.sendall(headers)
            response = HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 405)
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertTrue(response.read())
            response.close()

    def test_even_empty_transfer_encoding_is_rejected_without_reading_body(self):
        with running_server(self.root) as (server, _thread), socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
            headers = (f"POST /api/recommend HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding:\r\n\r\n").encode("ascii")
            connection.sendall(headers)
            response = HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["code"], "invalid_request")
            response.close()

    def test_reject_cleanup_never_reads_ambiguous_large_or_malformed_framing(self):
        class Connection:
            def gettimeout(self):
                return 15
            def settimeout(self, timeout):
                pass
        for pairs in (
            [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
            [("Content-Length", "2"), ("Transfer-Encoding", "")],
            [("Content-Length", "2"), ("Content-Length", "2")],
            [("Content-Length", str(lab.MAX_REQUEST_BYTES + 2))], [("Content-Length", "-2")],
            [("Content-Length", "+2")], [("Content-Length", "garbage")], [],
        ):
            handler = object.__new__(lab.LabHandler)
            handler.headers = Message()
            for key, value in pairs:
                handler.headers[key] = value
            handler.connection = Connection()
            handler.rfile = io.BytesIO(b"not forwarded")
            with self.subTest(headers=pairs):
                self.assertEqual(handler._discard_small_rejected_body(), 0)
                self.assertEqual(handler.rfile.tell(), 0)

    def test_static_symlink_is_never_served(self):
        target = self.root / "web/google-lab/lab.js"
        target.unlink()
        try:
            target.symlink_to(self.root / ".local/secret")
        except (OSError, NotImplementedError):
            self.skipTest("Creating filesystem symlinks is unavailable on this Windows host")
        with running_server(self.root) as (server, _thread):
            self.assertEqual(request(server, "/lab.js")[0], 404)

    def test_context_manager_stops_owned_server_and_closes_socket(self):
        with running_server(self.root) as (server, thread):
            self.assertEqual(request(server, "/api/health")[0], 200)
        self.assertFalse(thread.is_alive())
        self.assertEqual(server.socket.fileno(), -1)

    def test_public_bind_and_invalid_ports_are_rejected_before_startup(self):
        for host, port in (("0.0.0.0", 8003), ("example.com", 8003), ("::1", 8003), ("127.0.0.1", True), ("127.0.0.1", -1), ("127.0.0.1", 65536)):
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                lab.create_server(host, port, root=self.root)



EXACT_MEGA = "У меня 45 минут возле Mega, хочу короткую прогулку"
LOCAL_ENV = {
    "LLM_BASE_URL": "http://127.0.0.1:8081/v1",
    "LLM_MODEL": "astana-local",
    "LLM_JSON_MODE": "schema",
    "LLM_TOKEN_PARAMETER": "max_tokens",
    "LLM_TIMEOUT_SECONDS": "120",
}


def post_json(server, payload):
    status, headers, raw = request(server, "/api/recommend", "POST",
                                   json.dumps(payload, ensure_ascii=False).encode(),
                                   {"Content-Type": "application/json"})
    return status, headers, json.loads(raw)


class GoogleLabContextTests(unittest.TestCase):
    def test_exact_natural_message_resolves_budget_and_documented_public_start(self):
        result = lab.resolve_request({"query": EXACT_MEGA})
        context = result["resolved_context"]
        self.assertEqual(result["minutes"], 45)
        self.assertEqual(result["origin"]["lat"], 51.0892)
        self.assertEqual(result["origin"]["lon"], 71.4074)
        self.assertEqual(context["origin_id"], "mega")
        self.assertEqual(context["origin_source"], "query_landmark")
        self.assertEqual(context["minutes_source"], "query")
        self.assertEqual(context["time_of_day_source"], "astana_current_time")

    def test_known_start_keeps_automatic_budget_options_and_unknown_start_is_rejected(self):
        for query, budgets in (("Хочу кофе у Mega", [60, 120, 240]),
                               ("Я у Mega, хочу прогулку 30–60 минут", [30, 45, 60]),
                               ("Прогулка 20 минут и кофе 30 минут возле Mega", [60, 120, 240])):
            context = lab.resolve_request({"query": query})["resolved_context"]
            self.assertEqual(context["budget_options"], budgets)
            self.assertEqual(context["minutes"], budgets[0])
            self.assertIsNotNone(context["origin"])
        for query in ("Хочу кофе", "У меня 45 минут возле неизвестной башни"):
            with self.subTest(query=query), self.assertRaises(lab.ContextError) as caught:
                lab.resolve_request({"query": query})
            self.assertEqual(caught.exception.code, "known_start_required")
            self.assertEqual(caught.exception.status, 422)
        result = lab.resolve_request({"query": "У меня 45 минут, старт Mega или Байтерек"})
        self.assertEqual(result["resolved_context"]["origin_id"], "mega")
        self.assertTrue(result["resolved_context"]["assumptions"])

    def test_text_context_has_priority_over_stale_legacy_controls(self):
        cases = [
            ({"query": EXACT_MEGA, "minutes": 60}, "minutes", 45),
            ({"query": EXACT_MEGA, "origin_id": "baiterek"}, "origin_id", "mega"),
            ({"query": "Ночью хочу прогулку 60 минут у Байтерека", "time_of_day": "day"}, "time_of_day", "night"),
            ({"query": EXACT_MEGA, "origin": {"lat": 51.1283, "lon": 71.4305, "label": "Явно выбранная точка"}}, "origin_id", "mega"),
            ({"query": "Я у Mega, хочу прогулку 30–60 минут", "minutes": 90}, "budget_options", [30, 45, 60]),
        ]
        for payload, field, expected in cases:
            context = lab.resolve_request(payload)["resolved_context"]
            self.assertEqual(context[field], expected)
            self.assertTrue(context["assumptions"])

    def test_explicit_custom_point_keeps_label_unless_text_start_has_priority(self):
        point = {"lat": 51.0898, "lon": 71.4078, "label": "Своя точка рядом с ориентиром"}
        context = lab.resolve_request({"query": "Хочу прогулку", "minutes": 45, "origin": point})["resolved_context"]
        self.assertEqual(context["origin"], point)
        self.assertIsNone(context["origin_id"])
        self.assertEqual(context["origin_source"], "explicit_coordinates")
        self.assertTrue(context["sensitive_origin"])
        context = lab.resolve_request({"query": EXACT_MEGA, "origin": point})["resolved_context"]
        self.assertEqual(context["origin_id"], "mega")
        with self.assertRaises(lab.ContextError):
            lab.resolve_request({"query": EXACT_MEGA, "origin": {"lat": 51.0892, "lon": 71.4074}})

    def test_hours_ranges_and_explicit_long_budgets_remain_exact(self):
        for text, minutes in (("2 часа", 120), ("1,5 часа", 90), ("восемь часов", 480), ("полчаса", 30), ("375 минут", 375)):
            self.assertEqual(lab.resolve_request({"query": "Прогулка " + text + " у Mega"})["minutes"], minutes)
        context = lab.resolve_request({"query": "Я у Mega, хочу гулять 5–8 часов", "minutes": 375})["resolved_context"]
        self.assertEqual(context["budget_options"], [300, 390, 480])
        for minutes in (10, 300, 375, 480):
            self.assertEqual(lab.resolve_request({"query": "Хочу гулять", "origin_id": "mega", "minutes": minutes})["minutes"], minutes)

    def test_invalid_types_boundaries_and_unrecognized_explicit_landmark(self):
        cases = [{"query": EXACT_MEGA, "minutes": value} for value in (True, 9, 481, 45.0, "45", None)] + [
            {"query": EXACT_MEGA, "origin_id": "unknown"},
            {"query": EXACT_MEGA, "origin_id": "mega", "origin": {"lat": 51.0892, "lon": 71.4074, "label": "Два источника"}},
            {"query": EXACT_MEGA, "time_of_day": []},
            {"query": EXACT_MEGA, "origin": {"lat": float("nan"), "lon": 71.4, "label": "Некорректная"}},
            {"query": EXACT_MEGA, "origin": {"lat": True, "lon": 71.4, "label": "Некорректная"}},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(lab.ContextError):
                lab.resolve_request(payload)
        with self.assertRaises(lab.ContextError) as caught:
            lab.resolve_request({"query": "Хочу прогулку возле незнакомого отеля", "minutes": 60, "origin_id": "river"})
        self.assertEqual(caught.exception.code, "known_start_required")


class GoogleLabAITests(unittest.TestCase):
    setUp = GoogleLabTests.setUp
    write_fixture = GoogleLabTests.write_fixture

    def test_lab_uses_full_filtered_enriched_core_index_and_exact_mega_budget(self):
        with lab.create_server(port=0) as server:
            response = server.recommend({"query": EXACT_MEGA, "time_of_day": "day"})
            self.assertEqual(len(server.retriever.places), 1266)
            self.assertNotIn("osm:way/1419934935", server.by_id)
            self.assertEqual(response["plan"]["budget_minutes"], 45)
            self.assertEqual(response["plan"]["total_minutes"], 42)
            self.assertTrue(response["plan"]["returns_to_origin"])
            self.assertEqual([item["id"] for item in response["recommendations"]], ["osm:way/1467797207"])
            self.assertEqual(response["mode"], "extractive")
            self.assertEqual(response["diagnostics"]["llm_attempts"], 0)
            self.assertEqual(response["recommendations"][0]["source"], "OSM")
            self.assertEqual({item["id"] for item in response["recommendations"]}, {item["id"] for item in response["sources"]})
            self.assertIn("raw_provenance", response["recommendations"][0])

    def test_family_tourist_socket_and_night_keep_core_constraints_and_sources(self):
        cases = [
            ("Гуляю с ребёнком, ищу детскую площадку или парк", 60, "river", "day"),
            ("Я турист, хочу познакомиться с архитектурой", 120, "baiterek", "day"),
            ("Хочу тихое кафе с розеткой", 60, "mega", "day"),
            ("Ночью хочу найти активность рядом", 60, "baiterek", "night"),
        ]
        with lab.create_server(port=0) as server:
            for query, minutes, origin, day in cases:
                with self.subTest(query=query):
                    response = server.recommend({"query": query, "minutes": minutes, "origin_id": origin, "time_of_day": day})
                    selected = response["recommendations"]
                    self.assertLessEqual(response["plan"]["total_minutes"], minutes)
                    self.assertLessEqual(len(selected), 3)
                    self.assertEqual({p["id"] for p in selected}, {p["id"] for p in response["sources"]})
                    self.assertTrue(all(p["id"] in server.by_id for p in selected))
                    if "ребёнком" in query:
                        self.assertTrue(selected)
                        self.assertTrue(all(p["category"] in {"park", "playground"} for p in selected))
                    if "розеткой" in query:
                        self.assertEqual(selected, [])
                        self.assertTrue(response["empty_message"])
                        self.assertNotIn("clarification", response)
                    if day == "night":
                        self.assertTrue(all(server.by_id[p["id"]]["opening_hours"] == "24/7" for p in selected))

    def test_rag_enrichment_is_source_text_not_invented_alias_or_catalog_mutation(self):
        catalog = lab.load_catalog(self.root)
        before = deepcopy(catalog)
        document = lab.rag_document(catalog)
        cafe = document["places"][0]
        self.assertIn("regional;coffee_shop", cafe["description"])
        self.assertIn("Оператор из источника", cafe["description"])
        self.assertEqual(cafe.get("aliases"), catalog["places"][0].get("aliases"))
        self.assertEqual(catalog, before)
        index = lab.Retriever(data=document)
        result = index.retrieve("regional", {"lat": cafe["lat"], "lon": cafe["lon"]}, 60)
        self.assertEqual(result["selected"][0]["poi"]["id"], cafe["id"])

    def test_constructor_injection_receives_resolved_inputs_and_no_shared_server(self):
        observed = []
        def recommender(index, **kwargs):
            observed.append((index, deepcopy(kwargs)))
            return lab.core_recommend(index, **kwargs)
        with running_server(self.root, recommender=recommender) as (server, _thread):
            status, _, body = post_json(server, {"query": EXACT_MEGA})
            self.assertEqual(status, 200)
            self.assertEqual(observed[0][1]["minutes"], 45)
            self.assertEqual(observed[0][1]["origin"]["lat"], 51.0892)
            self.assertFalse(observed[0][1]["use_llm"])
            self.assertEqual(body["resolved_context"]["origin_source"], "query_landmark")
            self.assertIs(observed[0][0], server.retriever)

    def test_http_context_probe_is_local_and_malformed_inputs_never_call_recommender(self):
        with running_server(self.root, recommender=unittest.mock.Mock(side_effect=AssertionError("Must resolve first"))) as (server, _thread):
            for payload in ({"query": ""}, {"query": EXACT_MEGA, "minutes": True},
                            {"query": "Хочу кофе", "geolocation": {"lat": True, "lon": 71.4}}):
                status, headers, body = post_json(server, payload)
                self.assertEqual(status, 400)
                self.assertTrue(body["fields"])
                self.assertEqual(headers["Cache-Control"], "no-store")
            for payload, expected in (({"query": "Хочу кофе"}, True),
                                      ({"query": EXACT_MEGA}, False)):
                status, headers, raw = request(server, "/api/context", "POST", json.dumps(payload).encode(), {"Content-Type": "application/json"})
                result = json.loads(raw)
                self.assertEqual(status, 200)
                self.assertEqual(result["needs_geolocation"], expected)
                self.assertNotIn("query", result)
                self.assertEqual(headers["Cache-Control"], "no-store")
            for path in ("/api/context", "/api/recommend"):
                payload = {"query": "Хочу кофе", "location_status": "denied"}
                status, headers, raw = request(server, path, "POST", json.dumps(payload).encode(), {"Content-Type": "application/json"})
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(raw)["code"], "known_start_required")
                self.assertEqual(headers["Cache-Control"], "no-store")
            server.recommender.assert_not_called()

    def test_http_post_rejects_bad_bodies_mime_and_cross_origin(self):
        log = io.StringIO()
        with redirect_stderr(log), running_server(self.root) as (server, _thread):
            for raw in (b"{", b"\xff", b'{"query":"a","query":"b"}', b'{"query":NaN}', b'{"minutes":1e999}'):
                status, _, body = request(server, "/api/recommend", "POST", raw, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["code"], "invalid_json")
            self.assertEqual(request(server, "/api/recommend", "POST", b"x" * (lab.MAX_REQUEST_BYTES + 1), {"Content-Type": "application/json"})[0], 413)
            self.assertEqual(request(server, "/api/recommend", "POST", b"{}", {"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(request(server, "/api/recommend", "POST", b"{}", {"Content-Type": "application/json", "Origin": "https://foreign.example"})[0], 403)
            self.assertEqual(request(server, "/api/recommend", "POST", b"{}", {"Content-Type": "application/json", "Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(request(server, "/api/2gis/search", "POST", b"{}")[0], 404)
        self.assertEqual(log.getvalue(), "")

    @patch.dict("os.environ", LOCAL_ENV, clear=True)
    def test_llm_opt_in_startup_and_health_never_run_inference(self):
        with patch("app.llm.open_url", side_effect=AssertionError("No startup inference")) as provider:
            with running_server(self.root, llm_enabled=True) as (server, _thread):
                status, _, body = request(server, "/api/health")
                health = json.loads(body)
                self.assertEqual(status, 200)
                self.assertTrue(health["llm_configured"])
                self.assertEqual(health["provider_calls"], 0)
                self.assertFalse(health["live_verified"])
            provider.assert_not_called()

    def test_cloud_endpoints_and_keys_are_rejected_before_serving(self):
        for configuration in (
            {**LOCAL_ENV, "LLM_BASE_URL": "https://cloud.example/v1", "LLM_API_KEY": "fake"},
            {**LOCAL_ENV, "LLM_API_KEY": "fake"},
        ):
            with self.subTest(configuration=configuration), patch.dict("os.environ", configuration, clear=True), patch("app.llm.open_url") as provider:
                with self.assertRaises(ValueError):
                    lab.create_server(port=0, root=self.root, llm_enabled=True)
                provider.assert_not_called()

    @patch.dict("os.environ", LOCAL_ENV, clear=True)
    def test_changed_environment_cannot_redirect_an_existing_server_to_cloud(self):
        with running_server(self.root, llm_enabled=True) as (server, _thread), patch("app.llm.open_url") as provider:
            with patch.dict("os.environ", {"LLM_BASE_URL": "https://cloud.example/v1", "LLM_API_KEY": "fake"}):
                status, _, body = post_json(server, {"query": "Хочу парк", "minutes": 60, "origin": {"lat": 51.104, "lon": 71.4, "label": "Явная тестовая точка"}})
                self.assertEqual(status, 200)
                self.assertEqual(body["mode"], "extractive")
                self.assertEqual(body["diagnostics"]["llm_attempts"], 0)
                self.assertTrue(any("изменилось" in warning for warning in body["warnings"]))
            provider.assert_not_called()

    @patch.dict("os.environ", LOCAL_ENV, clear=True)
    def test_mocked_grounded_model_preserves_sources_and_hides_raw_query_origin(self):
        good = {"recommendations": [{"id": "osm:node/4", "why": "Парк соответствует интересу в прогулке на воздухе.", "action": "Идея: сделать короткую остановку в парке.", "citations": ["osm:node/4"], "fact_keys": ["category"]}]}
        envelope = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(good)}}]}).encode()
        with running_server(self.root, llm_enabled=True) as (server, _thread), patch("app.llm.open_url", return_value=io.BytesIO(envelope)) as provider:
            status, _, response = post_json(server, {"query": "PRIVATE_QUERY_98765 Хочу парк", "minutes": 60, "origin": {"lat": 51.104, "lon": 71.4, "label": "PRIVATE_ORIGIN_LABEL"}, "time_of_day": "day"})
            self.assertEqual(status, 200)
            self.assertEqual(response["mode"], "llm-grounded")
            self.assertEqual([p["id"] for p in response["recommendations"]], ["osm:node/4"])
            self.assertEqual(response["plan"]["total_minutes"], 20)
            self.assertEqual(response["diagnostics"]["llm_attempts"], 1)
            body = provider.call_args.args[0].data.decode()
            for secret in ("PRIVATE_QUERY_98765", "PRIVATE_ORIGIN_LABEL", "51.104", "71.4"):
                self.assertNotIn(secret, body)
            context = json.loads(json.loads(body)["messages"][1]["content"])
            self.assertEqual(set(context), {"intent", "retrieved_poi_facts"})
            self.assertEqual(set(context["retrieved_poi_facts"]), {"osm:node/4"})
            self.assertEqual(json.loads(server.health_json)["provider_calls"], 1)

    @patch.dict("os.environ", LOCAL_ENV, clear=True)
    def test_mocked_model_unknown_ids_repair_then_honest_fallback(self):
        bad = {"recommendations": [{"id": "invented", "why": "Парк соответствует интересу в прогулке на воздухе.", "action": "Идея: сделать короткую остановку в парке.", "citations": ["invented"], "fact_keys": ["category"]}]}
        raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad)}}]}).encode()
        with running_server(self.root, llm_enabled=True) as (server, _thread), patch("app.llm.open_url", side_effect=lambda *_: io.BytesIO(raw)) as provider:
            status, _, response = post_json(server, {"query": "Хочу парк", "minutes": 60, "origin": {"lat": 51.104, "lon": 71.4, "label": "Тестовая точка"}, "time_of_day": "day"})
            self.assertEqual(status, 200)
            self.assertEqual(response["mode"], "extractive")
            self.assertEqual(response["diagnostics"]["llm_status"], "validation_failed")
            self.assertEqual(response["diagnostics"]["llm_attempts"], 2)
            self.assertEqual(provider.call_count, 2)
            self.assertEqual([item["id"] for item in response["recommendations"]], ["osm:node/4"])
            self.assertTrue(any("LLM недоступна" in warning for warning in response["warnings"]))

    @patch.dict("os.environ", LOCAL_ENV, clear=True)
    def test_missing_budget_builds_three_actual_plans_and_only_primary_uses_model(self):
        good = {"recommendations": [{"id": "osm:node/4", "why": "Парк соответствует интересу в прогулке на воздухе.", "action": "Идея: сделать короткую остановку в парке.", "citations": ["osm:node/4"], "fact_keys": ["category"]}]}
        envelope = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(good)}}]}).encode()
        calls = []
        def recommender(index, **kwargs):
            calls.append(deepcopy(kwargs))
            return lab.core_recommend(index, **kwargs)
        with lab.create_server(port=0, root=self.root, llm_enabled=True, recommender=recommender) as server, patch("app.llm.open_url", return_value=io.BytesIO(envelope)) as provider:
            response = server.recommend({"query": "Хочу парк", "geolocation": {"lat": 51.104, "lon": 71.4}, "time_of_day": "day"})
            self.assertEqual([call["minutes"] for call in calls], [60, 120, 240])
            self.assertEqual([call["use_llm"] for call in calls], [True, False, False])
            self.assertEqual(provider.call_count, 1)
            variants = response["alternatives"]
            self.assertEqual([item["budget_minutes"] for item in variants], [60, 120, 240])
            self.assertEqual([item["mode"] for item in variants], ["llm-grounded", "extractive", "extractive"])
            for item in variants:
                self.assertNotIn("alternatives", item)
                self.assertEqual(item["plan"]["budget_minutes"], item["budget_minutes"])
                self.assertEqual(item["resolved_context"]["minutes"], item["budget_minutes"])
                self.assertLessEqual(item["plan"]["total_minutes"], item["budget_minutes"])
                self.assertTrue(item["plan"]["returns_to_origin"])
                self.assertTrue(item["recommendations"])
            self.assertIsNot(response, variants[0])

    def test_budget_profiles_replan_three_nearby_places_without_mutating_source_or_exact_budget(self):
        template = deepcopy(self.base_places[3])
        self.base_places, self.elements = [], []
        for index, latitude in enumerate((51.1009, 51.1018, 51.1027), 1):
            place = deepcopy(template)
            name = "Синтетический парк " + str(index)
            place.update(id=f"osm:node/{index}", name=name, source_name=name, name_generated=False,
                         lat=latitude, lon=71.4, source_url=f"https://www.openstreetmap.org/node/{index}")
            self.base_places.append(place)
            self.elements.append({"type": "node", "id": index, "tags": {"name": name, "leisure": "park"}})
        self.write_fixture()
        calls = []
        def recommender(index, **kwargs):
            calls.append((index, deepcopy(kwargs)))
            return lab.core_recommend(index, **kwargs)
        with lab.create_server(port=0, root=self.root, recommender=recommender) as server, patch("app.llm.open_url", side_effect=AssertionError("No actual model")) as provider:
            before_catalog = deepcopy(server.catalog)
            before_places = deepcopy(server.retriever.places)
            base_index = server.retriever
            form = {"query": "Хочу парк", "geolocation": {"lat": 51.1, "lon": 71.4}, "time_of_day": "day"}
            result = server.recommend(form)
            variants = result["alternatives"]
            self.assertEqual([item["budget_minutes"] for item in variants], [60, 120, 240])
            self.assertEqual([item["plan"]["visit_minutes"] for item in variants], [45, 90, 180])
            self.assertEqual(len({item["plan"]["total_minutes"] for item in variants}), 3)
            self.assertEqual(len({id(index) for index, _ in calls}), 3)
            self.assertTrue(all(index is not base_index for index, _ in calls))
            for item, proposal in zip(variants, (15, 30, 60)):
                self.assertEqual(len(item["recommendations"]), 3)
                self.assertEqual([place["visit_minutes"] for place in item["recommendations"]], [proposal] * 3)
                self.assertTrue(all(str(proposal) in place["action"] for place in item["recommendations"]))
                self.assertTrue(item["plan"]["returns_to_origin"])
                self.assertLessEqual(item["plan"]["total_minutes"], item["budget_minutes"])
                self.assertEqual(item["diagnostics"]["time_allocation"], item["resolved_context"]["time_allocation"])
                self.assertEqual(item["diagnostics"]["time_allocation"]["proposed_visit_minutes_per_stop"], proposal)
                self.assertIs(item["diagnostics"]["time_allocation"]["is_source_fact"], False)
                self.assertEqual(item["diagnostics"]["lab_base_index_sha256"], server.index_sha256)
                self.assertNotEqual(item["diagnostics"]["lab_index_sha256"], server.index_sha256)
                self.assertTrue(any("предложено приложением" in warning and "OSM не подтверждает" in warning for warning in item["warnings"]))
            exact = server.recommend({**form, "minutes": 120})
            self.assertIs(calls[-1][0], base_index)
            self.assertEqual([place["visit_minutes"] for place in exact["recommendations"]], [20] * 3)
            self.assertEqual(exact["diagnostics"]["lab_index_sha256"], server.index_sha256)
            self.assertEqual(exact["resolved_context"]["time_allocation"]["policy"], "base_visit_profile")
            self.assertEqual(server.catalog, before_catalog)
            self.assertEqual(server.retriever.places, before_places)
            self.assertIs(server.retriever, base_index)
            provider.assert_not_called()

    def test_generic_walk_and_empty_results_never_ask_manual_context(self):
        with lab.create_server(port=0, root=self.root) as server:
            response = server.recommend({"query": "Хочу что-то необычное, пешком", "minutes": 60,
                                         "geolocation": {"lat": 51.104, "lon": 71.4}, "time_of_day": "day"})
            self.assertTrue(response["recommendations"])
            # "Пешком" is recognized directly by the shared activity catalog.
            self.assertFalse(response["diagnostics"]["lab_generic_walk_fallback"])
            empty = server.recommend({"query": "Хочу тихое кафе с розеткой", "minutes": 60,
                                      "origin_id": "mega", "time_of_day": "day"})
            self.assertEqual(empty["recommendations"], [])
            self.assertNotIn("clarification", empty)
            self.assertTrue(empty["empty_message"])
            self.assertNotRegex(empty["empty_message"], "Уточните|Увеличьте|выберите")

    def test_explicit_long_budget_reaches_the_core_without_promising_to_fill_it(self):
        with lab.create_server(port=0) as server:
            for minutes in (10, 375, 480):
                response = server.recommend({"query": "Хочу прогулку", "minutes": minutes, "origin_id": "mega"})
                self.assertEqual(response["plan"]["budget_minutes"], minutes)
                self.assertLessEqual(response["plan"]["total_minutes"], minutes)
                self.assertLessEqual(len(response["recommendations"]), 3)
                if minutes >= 375:
                    self.assertLess(response["plan"]["total_minutes"], minutes)
                if not response["recommendations"]:
                    self.assertTrue(response["empty_message"])
                    self.assertNotIn("clarification", response)

    def test_before_flight_warning_does_not_claim_airport_transfer_in_the_plan(self):
        with lab.create_server(port=0) as server:
            response = server.recommend({"query": "Я турист, у меня 2 часа перед вылетом, хочу посмотреть архитектуру", "origin_id": "baiterek"})
            self.assertEqual(response["resolved_context"]["minutes"], 120)
            self.assertLessEqual(response["plan"]["total_minutes"], 120)
            self.assertTrue(any("Дорога в аэропорт" in warning and "не входят" in warning for warning in response["warnings"]))
            self.assertTrue(all(item["id"] in server.by_id for item in response["recommendations"]))


    def test_unexpected_recommender_failure_is_sanitized_and_does_not_log_input(self):
        def fail(*_args, **_kwargs):
            raise RuntimeError("PRIVATE_INTERNAL_DETAIL")
        output = io.StringIO()
        with redirect_stderr(output), running_server(self.root, recommender=fail) as (server, _thread):
            status, _, response = post_json(server, {"query": EXACT_MEGA})
            self.assertEqual(status, 500)
            self.assertEqual(response["code"], "recommendation_failed")
            self.assertNotIn("PRIVATE_INTERNAL_DETAIL", json.dumps(response))
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
