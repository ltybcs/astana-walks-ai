"""Official Google protocol tests with an in-memory provider; zero network."""
from copy import deepcopy
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from app.google_budget import GoogleBudget, GoogleError
from app.google_client import GoogleClient, DETAIL_MASKS, MCP_URL, PLACES_URL, _NoRedirect, json_bytes, strict_json


class Response(io.BytesIO):
    def __init__(self, value=b"", *, mime="application/json", status=200, extra=None):
        super().__init__(value if isinstance(value, bytes) else json_bytes(value))
        self.headers = Message()
        self.headers["Content-Type"] = mime
        for key, item in (extra or {}).items():
            self.headers[key] = item
        self.status = status

    def read1(self, size=-1):
        return self.read(size)


def parameters(text="safe public search"):
    return {"textQuery": text, "languageCode": "ru", "regionCode": "KZ",
            "locationBias": {"circle": {"center": {"latitude": 51.0892, "longitude": 71.4074}, "radiusMeters": 1050}},
            "pageSize": 5, "includeExtendedDetails": False}


def schemas(snake=False):
    text, language, region = "textQuery", "languageCode", "regionCode"
    mode, identifier = ("travel_mode", "place_id") if snake else ("travelMode", "placeId")
    point = {"type": "object", "properties": {"address": {"type": "string"}, identifier: {"type": "string"}}}
    return [{"name": "search_places", "inputSchema": {"type": "object", "properties": {text: {"type": "string"}, language: {"type": "string"}, region: {"type": "string"}, "locationBias": {"type": "object"}, "pageSize": {"type": "integer"}, "includeExtendedDetails": {"type": "boolean"}}, "required": [text]}},
            {"name": "compute_routes", "inputSchema": {"type": "object", "properties": {"origin": point, "destination": point, mode: {"type": "string", "enum": ["DRIVE", "WALK"]}}, "required": ["origin", "destination"]}}]


class Provider:
    def __init__(self, *, snake=False, sse=False):
        self.requests = []
        self.tools = schemas(snake)
        self.sse = sse
        self.route_duration = "600s"
        self.search = {"summary": "Тестовый парк для прогулки [0].", "places": [{"id": "mock-place", "place": "places/mock-place", "location": {"latitude": 51.09, "longitude": 71.41}, "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/place/?q=place_id:mock-place"}, "attribution": {"title": "Тестовый источник", "url": "https://www.google.com/maps"}}]}

    def __call__(self, request, timeout):
        body = strict_json(request.data) if request.data else None
        self.requests.append((request, body, timeout))
        if request.full_url.startswith(PLACES_URL):
            return Response({"id": "mock-place", "displayName": {"text": "Тестовое имя"}, "attributions": []})
        method = body["method"]
        if method == "notifications/initialized":
            return Response(status=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": self.tools}
        else:
            name = body["params"]["name"]
            value = self.search if name == "search_places" else {"routes": [{"distanceMeters": 700, "duration": self.route_duration, "attribution": {"title": "Google Maps", "url": "https://www.google.com/maps"}}]}
            result = {"structuredContent": deepcopy(value), "isError": False}
        rpc = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            payload = b": heartbeat\n\n" + b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n' + b"data: " + json_bytes(rpc) + b"\n\n"
            return Response(payload, mime="text/event-stream", extra={"Mcp-Session-Id": "mock-session"})
        return Response(rpc, extra={"Mcp-Session-Id": "mock-session"})


class GoogleClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.json"
        GoogleBudget.initialize(self.path)
        self.budget = GoogleBudget(self.path)

    def client(self, provider):
        return GoogleClient("MOCK_KEY_NOT_A_CREDENTIAL", self.budget, http_open=provider)

    def test_mcp_lifecycle_json_and_sse_and_declared_case_variants(self):
        for snake, sse in ((False, False), (True, True)):
            with self.subTest(snake=snake, sse=sse):
                provider = Provider(snake=snake, sse=sse)
                client = self.client(provider)
                self.assertEqual(client.search_places(parameters()), provider.search)
                outward = client.compute_route({"address": "Public Astana"}, {"id": "mock-place"})
                inward = client.compute_route({"id": "mock-place"}, {"address": "Public Astana"})
                self.assertEqual(outward, inward)
                methods = [body["method"] for _, body, _ in provider.requests]
                self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call", "tools/call"])
                self.assertEqual(provider.requests[0][0].get_header("X-goog-api-key"), "MOCK_KEY_NOT_A_CREDENTIAL")
                self.assertEqual(provider.requests[3][0].get_header("Mcp-session-id"), "mock-session")
                self.assertEqual(provider.requests[3][0].get_header("Mcp-protocol-version"), "2025-06-18")
                self.assertEqual(provider.requests[3][1]["params"]["arguments"], parameters())
                args = provider.requests[4][1]["params"]["arguments"]
                self.assertEqual(args["travel_mode" if snake else "travelMode"], "WALK")
                self.assertEqual(provider.requests[5][1]["params"]["arguments"]["origin"], {"place_id" if snake else "placeId": "mock-place"})
                self.assertTrue(all(request.full_url == MCP_URL for request, _, _ in provider.requests))
        self.assertEqual(self.budget.status()["used"], 12)

    def test_search_requires_complete_exact_preview_parameters_before_network(self):
        provider = Provider()
        client = self.client(provider)
        for value in ("raw string query", {**parameters(), "pageSize": 6},
                      {**parameters(), "includeExtendedDetails": True},
                      {key: value for key, value in parameters().items() if key != "locationBias"}):
            with self.subTest(value=value), self.assertRaises(GoogleError):
                client.search_places(value)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)

    def test_details_fixed_profiles_masks_and_no_mcp_or_llm(self):
        provider = Provider()
        client = self.client(provider)
        for profile in ("hours", "reviews"):
            result = client.details("mock-place", profile)
            self.assertEqual(result["source"], "google_places")
            self.assertEqual(result["profile"], profile)
            request, body, _ = provider.requests[-1]
            self.assertEqual(request.get_header("X-goog-fieldmask"), DETAIL_MASKS[profile])
            self.assertNotIn("*", DETAIL_MASKS[profile])
            self.assertEqual(request.full_url, PLACES_URL + "mock-place?languageCode=ru")
            self.assertIsNone(body)
            self.assertNotIn("MOCK_KEY", request.full_url)
        self.assertEqual(self.budget.status()["used"], 2)

    def test_budget_error_during_initialization_retains_code_and_no_http(self):
        self.path.write_bytes(b"corrupt")
        provider = Provider()
        with self.assertRaises(GoogleError) as caught:
            self.client(provider).search_places(parameters("public"))
        self.assertEqual((caught.exception.code, caught.exception.status), ("quota_unavailable", 503))
        self.assertEqual(provider.requests, [])

    def test_failed_request_is_reserved_and_error_body_key_never_leaks(self):
        secret = "MOCK_SECRET_BODY"
        def fail(request, timeout):
            raise HTTPError(request.full_url, 429, secret, {}, io.BytesIO(secret.encode()))
        with self.assertRaises(GoogleError) as caught:
            self.client(fail).search_places(parameters("safe"))
        self.assertEqual((caught.exception.code, caught.exception.status), ("google_rate_limited", 429))
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.budget.status()["used"], 1)

    def test_protocol_bad_id_oversize_duplicate_and_unexpected_tool_are_rejected(self):
        for raw, mime in ((b'{"jsonrpc":"2.0","id":999,"result":{}}', "application/json"),
                          (b'{"a":1,"a":2}', "application/json"),
                          (b"x" * 1_048_577, "application/json"),
                          (b"data: " + b"x" * 65537 + b"\n\n", "text/event-stream"),
                          (b"data: {}\n\n", "text/event-stream")):
            with self.subTest(mime=mime, size=len(raw)), self.assertRaises(GoogleError):
                self.client(lambda request, timeout: Response(raw, mime=mime)).search_places(parameters("public"))
        provider = Provider()
        provider.tools[0]["inputSchema"]["properties"] = {"guess": {"type": "string"}}
        with self.assertRaises(GoogleError):
            self.client(provider).search_places(parameters("public"))
        self.assertEqual(len(provider.requests), 3)

    def test_no_implicit_redirect_retry_or_calls_at_status(self):
        provider = Provider()
        client = self.client(provider)
        self.assertEqual(client.status()["quota"]["used"], 0)
        self.assertEqual(provider.requests, [])
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "x", {}, "https://other.invalid"))
        def offline(request, timeout):
            raise URLError("secret diagnostic")
        with self.assertRaises(GoogleError):
            self.client(offline).details("mock-place", "hours")
        self.assertEqual(self.budget.status()["used"], 1)
        with self.assertRaises(GoogleError):
            client.details("https://other.invalid", "hours")
        self.assertEqual(provider.requests, [])

    def test_shared_deadline_refuses_more_network_before_reservation(self):
        provider = Provider()
        client = self.client(provider)
        with client.workflow(-1):
            with self.assertRaises(GoogleError) as caught:
                client.search_places(parameters("public"))
        self.assertEqual(caught.exception.code, "google_timeout")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.budget.status()["used"], 0)

    def test_runtime_config_uses_explicit_test_files_and_never_initializes(self):
        directory = Path(self.temporary.name)
        key_path = directory / "mock-key"
        key_path.write_text("MOCK_KEY_NOT_REAL", encoding="utf-8")
        config = directory / "config.json"
        config.write_bytes(json_bytes({"key_file": str(key_path), "ledger_file": str(directory / "missing-ledger.json")}))
        client = GoogleClient.from_runtime(config)
        self.assertEqual(client.status()["quota"]["state"], "unavailable")
        self.assertFalse((directory / "missing-ledger.json").exists())
        self.assertNotIn("MOCK_KEY_NOT_REAL", repr(client))

    def test_strict_decoder_rejects_overflow_duplicates_nan_and_non_utf8(self):
        for raw in (b'{"x":1e999}', b'{"x":NaN}', b'{"x":1,"x":2}', '{"x":1}'.encode("utf-16")):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                strict_json(raw)


if __name__ == "__main__":
    unittest.main()
