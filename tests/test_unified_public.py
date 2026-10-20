"""Public tunnel boundary tests. Every service is local and provider calls are mocked."""
from email.message import Message
from http.client import HTTPResponse
import json
import socket
import unittest
from unittest.mock import Mock, patch

from app import google_lab as lab
from app.google_grounded import GoogleGrounded
from tests import test_google_lab as lab_tests
running_server, request = lab_tests.running_server, lab_tests.request
from tests.test_unified_walks import Google

PUBLIC = "https://walks.example.test"
PUBLIC_HOST = "walks.example.test"


class UnifiedPublicTests(unittest.TestCase):
    setUp = lab_tests.GoogleLabTests.setUp
    write_fixture = lab_tests.GoogleLabTests.write_fixture

    def wire(self, server, path, pairs, *, method="GET", body=b""):
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in pairs)
            sock.sendall(head.encode("ascii") + b"Connection: close\r\n\r\n" + body)
            response = HTTPResponse(sock)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def test_public_setting_validates_before_files_runtime_or_binding(self):
        invalid = ("", True, "http://walks.example.test", "https://localhost", "https://host.local",
                   "https://127.0.0.1", "https://[::1]", "https://127.1", "https://2130706433",
                   "https://*.example.test", "https://user:pass@walks.example.test",
                   "https://walks.example.test/", "https://walks.example.test/path",
                   "https://walks.example.test?x=1", "https://walks.example.test#fragment",
                   "https://walks.example.test?", "https://walks.example.test#",
                   "https://walks.example.test:0", "https://walks.example.test:99999",
                   "https://walks.example.test:443", "https://Walks.Example.Test", "https://a..test",
                   "https://-a.test", "https://a_.test", "https://walks.example.test ")
        with patch("app.google_lab.load_catalog", side_effect=AssertionError("Validate first")), \
             patch("app.google_lab.GoogleClient.from_runtime", side_effect=AssertionError("No key read")):
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    lab.create_server(port=0, root=self.root, public_origin=value, google_config="DO_NOT_READ")
        self.assertEqual(lab._public_origin(PUBLIC + ":8443"), PUBLIC + ":8443")
        with self.assertRaises(ValueError):
            lab.create_server(host="0.0.0.0", port=0, root=self.root, public_origin=PUBLIC)

    def test_exact_public_and_local_host_origin_pairs_only_and_forwarded_ignored(self):
        with running_server(self.root, public_origin=PUBLIC) as (server, _):
            local = f"127.0.0.1:{server.server_port}"
            cases = [([( "Host", PUBLIC_HOST), ("Origin", PUBLIC)], 200),
                     ([("Host", PUBLIC_HOST)], 200),
                     ([("Host", local), ("Origin", "http://" + local)], 200),
                     ([("Host", local)], 200),
                     ([("Host", PUBLIC_HOST), ("Origin", "http://" + local)], 403),
                     ([("Host", local), ("Origin", PUBLIC)], 403),
                     ([("Host", local), ("Origin", f"http://localhost:{server.server_port}")], 403),
                     ([("Host", PUBLIC_HOST), ("Origin", "http://" + PUBLIC_HOST)], 403),
                     ([("Host", "attacker.test"), ("Origin", "https://attacker.test")], 403),
                     ([("Host", PUBLIC_HOST), ("Host", PUBLIC_HOST)], 403),
                     ([("Host", PUBLIC_HOST), ("Origin", PUBLIC), ("Origin", PUBLIC)], 403),
                     ([("Host", "attacker.test"), ("Forwarded", "host=" + PUBLIC_HOST + ";proto=https"),
                       ("X-Forwarded-Host", PUBLIC_HOST), ("X-Forwarded-Proto", "https")], 403),
                     ([("Host", PUBLIC_HOST), ("Origin", PUBLIC), ("Forwarded", "host=attacker.test;proto=http")], 200),
                     ([("Origin", PUBLIC)], 403)]
            for pairs, expected in cases:
                with self.subTest(pairs=pairs):
                    status, headers, _ = self.wire(server, "/", pairs)
                    self.assertEqual(status, expected)
                    self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_public_no_start_rejects_before_any_provider_status_search_or_model(self):
        client, dgis = Google(), Mock()
        google = GoogleGrounded(client, lambda _: None)
        with running_server(self.root, public_origin=PUBLIC, google=google, dgis=dgis) as (server, _):
            for query in ("Хочу кофе", "Хочу гулять"):
                form = {"query": query, "location_status": "denied", "consent": True}
                status, headers, raw = request(server, "/api/unified/recommend", "POST", json.dumps(form),
                                               {"Host": PUBLIC_HOST, "Origin": PUBLIC, "Content-Type": "application/json"})
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(raw)["code"], "known_start_required")
                self.assertEqual(headers["Cache-Control"], "no-store")
            probe = {"query": "Хочу гулять"}
            status, _, raw = request(server, "/api/context", "POST", json.dumps(probe),
                                     {"Host": PUBLIC_HOST, "Origin": PUBLIC, "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["needs_geolocation"])
            client.budget.status.assert_not_called()
            self.assertEqual(client.search_count, 0)
            dgis.status.assert_not_called()
            dgis.search.assert_not_called()
            self.assertEqual(server._provider_attempts, 0)

    def test_public_unknown_named_start_resolves_once_without_activity_search_or_model(self):
        # A typed unlisted start may use exactly one explicit Places resolution;
        # this exception never authorizes activity search from an unresolved point.
        client, dgis = Google(), Mock()
        client.resolve_place = Mock(return_value=[])
        google = GoogleGrounded(client, lambda _: None)
        with running_server(self.root, public_origin=PUBLIC, google=google, dgis=dgis) as (server, _):
            server.retriever.retrieve = Mock()
            server.unified.narrator = Mock()
            server.model_gateway.http_open = Mock()
            form = {"query": "Хочу кофе возле гостиницы Синтетическая", "consent": True}
            status, headers, raw = request(server, "/api/unified/recommend", "POST", json.dumps(form),
                {"Host": PUBLIC_HOST, "Origin": PUBLIC, "Content-Type": "application/json"})
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(raw)["code"], "location_unresolved")
            self.assertEqual(headers["Cache-Control"], "no-store")
            client.resolve_place.assert_called_once()
            self.assertEqual(client.search_count, 0)
            self.assertEqual(client.legs, [])
            client.budget.status.assert_not_called()
            dgis.status.assert_not_called()
            dgis.search.assert_not_called()
            server.retriever.retrieve.assert_not_called()
            server.unified.narrator.assert_not_called()
            # The model is unconfigured: deterministic preparation performs no I/O.
            self.assertIsNone(server.model_gateway.config)
            server.model_gateway.http_open.assert_not_called()
            self.assertEqual(server._provider_attempts, 0)

    def test_public_mode_disables_all_legacy_apis_even_with_local_host(self):
        client, dgis = Google(), Mock()
        google = GoogleGrounded(client, lambda _: None)
        with running_server(self.root, public_origin=PUBLIC, google=google, dgis=dgis) as (server, _):
            for headers in ({"Host": PUBLIC_HOST, "Origin": PUBLIC}, {}):
                for path in ("/api/catalog", "/api/google/status", "/embeds.json"):
                    self.assertEqual(request(server, path, headers=headers)[0], 404)
                for path in ("/api/recommend", "/api/google/preview", "/api/google/recommend", "/api/google/details", "/api/2gis/search"):
                    self.assertEqual(request(server, path, "POST", b"{}", {**headers, "Content-Type": "application/json"})[0], 404)
            self.assertEqual(client.search_count, 0)
            client.budget.status.assert_not_called()
            dgis.status.assert_not_called()
            dgis.search.assert_not_called()

    def test_default_local_mode_keeps_legacy_and_refuses_unconfigured_public_host(self):
        with running_server(self.root) as (server, _):
            self.assertEqual(request(server, "/api/catalog")[0], 200)
            self.assertEqual(request(server, "/embeds.json")[0], 200)
            self.assertEqual(request(server, "/", headers={"Host": PUBLIC_HOST, "Origin": PUBLIC})[0], 403)
            form = {"query": "Хочу кофе", "location_status": "denied"}
            status, _, raw = request(server, "/api/recommend", "POST", json.dumps(form), {"Content-Type": "application/json"})
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(raw)["code"], "known_start_required")

    def test_public_pwa_shell_has_exact_static_paths_no_api_cache_or_legacy_embed_catalog(self):
        with running_server(self.root, public_origin=PUBLIC) as (server, _):
            headers = {"Host": PUBLIC_HOST, "Origin": PUBLIC}
            for path in ("/", "/lab.js", "/lab.css", "/pwa.js", "/sw.js", "/manifest.webmanifest", "/favicon.svg",
                         "/icons/icon-192.png", "/icons/icon-512.png", "/icons/maskable-512.png", "/icons/apple-touch-icon.png"):
                status, response_headers, raw = request(server, path, headers=headers)
                self.assertEqual(status, 200, path)
                self.assertEqual(response_headers["Cache-Control"], "no-store")
                if path == "/manifest.webmanifest":
                    self.assertEqual(response_headers["Content-Type"], "application/manifest+json")
            for path in ("/.local/secret", "/.git/config", "/app/unified_walks.py", "/web/google-lab/", "/sw.js?private=1"):
                self.assertEqual(request(server, path, headers=headers)[0], 404)

    def test_cli_passes_explicit_public_origin_without_changing_loopback_bind(self):
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        server.server_port = 8003
        server.catalog = {"summary": {"visible_count": 1}}
        with patch("app.google_lab.create_server", return_value=server) as create, patch("builtins.print"):
            self.assertEqual(lab.main(["--public-origin", PUBLIC]), 0)
        self.assertEqual(create.call_args.args, ("127.0.0.1", 8003))
        self.assertEqual(create.call_args.kwargs["public_origin"], PUBLIC)


if __name__ == "__main__":
    unittest.main()
