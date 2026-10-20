"""Actual loopback HTTP checks, using a temporary port and no external LLM."""

import http.client
import json
import socket
import time
from email.message import Message
from threading import Thread
import unittest
from unittest.mock import patch, Mock

from app.server import create_server, Handler, MAX_REQUEST_BYTES
from tests.test_retrieval import ORIGIN, index, poi


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, retriever=index(poi("p")))
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def post(self, body, headers=None):
        return self.request("POST", "/api/recommend", body, headers or {"Content-Type": "application/json"})

    def test_valid_recommendation_and_no_sensitive_logging(self):
        payload = json.dumps({"query": "парк", "minutes": 60, "origin": ORIGIN}).encode()
        with patch.dict("os.environ", {}, clear=True), patch("sys.stderr") as stderr:
            status, headers, body = self.post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["recommendations"][0]["id"], "p")
        self.assertEqual(headers["Cache-Control"], "no-store")
        log = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertNotIn("POST /api/recommend", log)
        self.assertNotIn(str(ORIGIN["lat"]), log)
        self.assertNotIn('"query":', log)

    def test_health_reports_capabilities_without_claiming_live_verification(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["llm"]["live_verified"])

    def test_long_and_custom_duration_budget_reaches_the_planner(self):
        for minutes in (10, 180, 240, 300, 375, 480):
            with self.subTest(minutes=minutes), patch.dict("os.environ", {}, clear=True):
                status, _, body = self.post(json.dumps({"query": "парк", "minutes": minutes, "origin": ORIGIN}).encode())
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertEqual(result["plan"]["budget_minutes"], minutes)
                self.assertLessEqual(result["plan"]["total_minutes"], minutes)
                if minutes >= 180:
                    self.assertTrue(result["recommendations"])
                    self.assertGreater(result["plan"]["return_walk_minutes"], 0)

    def test_duration_outside_supported_range_is_rejected(self):
        for minutes in (9, 481, 300.5, "480", True):
            with self.subTest(minutes=minutes):
                status, _, body = self.post(json.dumps({"query": "парк", "minutes": minutes, "origin": ORIGIN}).encode())
                self.assertEqual(status, 400)
                self.assertIn("10 до 480", json.loads(body)["error"])

    def test_malformed_json_utf8_and_duplicate_keys_are_400(self):
        for body in (b"{", b"\xff", b'{"query":"a","query":"b"}', b'{"query":NaN}'):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)

    def test_invalid_input_types_and_enormous_coordinates_are_400(self):
        for origin in ({"lat": True, "lon": 71}, {"lat": 10 ** 500, "lon": 71}, {"lat": float("inf"), "lon": 71}):
            body = json.dumps({"query": "парк", "minutes": 60, "origin": origin}).encode()
            with self.subTest(origin=origin):
                self.assertEqual(self.post(body)[0], 400)

    def test_oversized_input_is_413(self):
        self.assertEqual(self.post(b"x" * 16_385)[0], 413)

    def test_delayed_one_byte_oversized_body_still_returns_http_413(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            header = (f"POST /api/recommend HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                      f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\nContent-Type: application/json\r\n\r\n")
            sock.sendall(header.encode("ascii"))
            time.sleep(.03)
            sock.sendall(b"x" * (MAX_REQUEST_BYTES + 1))
            response = http.client.HTTPResponse(sock)
            response.begin()
            self.assertEqual(response.status, 413)
            self.assertIn("слишком большой", json.loads(response.read())["error"])
            response.close()

    def test_maximum_accepted_json_size_remains_exact(self):
        body = json.dumps({"query": "парк", "minutes": 60, "origin": ORIGIN}).encode()
        body += b" " * (MAX_REQUEST_BYTES - len(body))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(self.post(body + b" ")[0], 413)

    def test_early_reject_never_drains_ambiguous_large_or_malformed_framing(self):
        for pairs in (
            [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
            [("Content-Length", "2"), ("Transfer-Encoding", "")],
            [("Content-Length", "2"), ("Content-Length", "2")],
            [("Content-Length", str(MAX_REQUEST_BYTES + 2))],
            [("Content-Length", "999999999999999999")],
            [("Content-Length", "+2")], [("Content-Length", "-2")], [],
        ):
            handler = object.__new__(Handler)
            handler.headers = Message()
            for key, value in pairs:
                handler.headers[key] = value
            handler.connection = Mock()
            handler.rfile = Mock()
            with self.subTest(headers=pairs):
                self.assertEqual(handler._discard_small_rejected_body(), 0)
                handler.rfile.read1.assert_not_called()
                handler.connection.gettimeout.assert_not_called()

    def test_early_reject_cleanup_uses_one_wall_deadline_not_per_chunk(self):
        elapsed = [0.0]
        handler = object.__new__(Handler)
        handler.headers = Message()
        handler.headers["Content-Length"] = "100"
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = 15
        handler.rfile = Mock()
        def trickle(length):
            elapsed[0] += .11
            return b"x" * min(10, length)
        handler.rfile.read1.side_effect = trickle
        with patch("app.server.monotonic", side_effect=lambda: elapsed[0]):
            self.assertEqual(handler._discard_small_rejected_body(), 20)
        self.assertEqual(handler.rfile.read1.call_count, 2)
        self.assertEqual(handler.connection.settimeout.call_args.args[0], 15)
        self.assertAlmostEqual(elapsed[0], .22)

    def test_wrong_mime_type_is_415(self):
        self.assertEqual(self.post(b"{}", {"Content-Type": "text/plain"})[0], 415)

    def test_cross_origin_and_malformed_origin_are_403(self):
        for origin in ("https://attacker.example", "http://["):
            self.assertEqual(self.post(b"{}", {"Content-Type": "application/json", "Origin": origin})[0], 403)

    def test_transfer_encoding_is_rejected(self):
        for value in ("chunked", ""):
            with self.subTest(value=value):
                self.assertEqual(self.post(b"{}", {"Content-Type": "application/json", "Transfer-Encoding": value})[0], 400)

    def test_static_paths_cannot_expose_repo_or_traverse(self):
        for path in ("/.env", "/../app/server.py", "/%2e%2e/.env", "/data/poi.sample.json", "/unknown"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
                self.assertEqual(self.request("HEAD", path)[0], 404)

    def test_unexpected_runtime_error_is_sanitized_and_next_request_works(self):
        payload = json.dumps({"query": "парк", "minutes": 60, "origin": ORIGIN}).encode()
        with patch("app.server.recommend", side_effect=RuntimeError("key-secret private-path")):
            status, _, body = self.post(payload)
        self.assertEqual(status, 500)
        self.assertNotIn(b"key-secret", body)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
