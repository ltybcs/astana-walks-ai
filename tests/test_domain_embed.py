"""Optional exact-parent framing; loopback HTTP fixtures, no provider/model calls."""
import json
import unittest
from unittest.mock import Mock, patch

from app import google_lab as lab
from tests import test_google_lab as fixtures

PARENT = "https://ltybcs.com"
PUBLIC = "https://demo.example.test"
HOST = "demo.example.test"


class DomainEmbedTests(unittest.TestCase):
    setUp = fixtures.GoogleLabTests.setUp
    write_fixture = fixtures.GoogleLabTests.write_fixture

    def test_default_remains_deny_for_get_and_head(self):
        with fixtures.running_server(self.root) as (server, _):
            for method in ("GET", "HEAD"):
                with self.subTest(method=method):
                    status, headers, body = fixtures.request(server, "/", method)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Security-Policy"], lab.CSP)
                    self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                    self.assertEqual(headers["X-Frame-Options"], "DENY")
                    if method == "HEAD": self.assertEqual(body, b"")

    def test_explicit_parent_changes_only_ancestor_policy_and_omits_xfo(self):
        expected = lab.CSP.replace("frame-ancestors 'none'", "frame-ancestors " + PARENT)
        with fixtures.running_server(self.root, public_origin=PUBLIC, frame_parent_origin=PARENT) as (server, _):
            for path in ("/", "/api/health"):
                for method in ("GET", "HEAD"):
                    with self.subTest(path=path, method=method):
                        status, headers, body = fixtures.request(server, path, method,
                            headers={"Host": HOST, "Origin": PUBLIC})
                        self.assertEqual(status, 200)
                        self.assertEqual(headers["Content-Security-Policy"], expected)
                        self.assertNotIn("X-Frame-Options", headers)
                        self.assertEqual(headers["Permissions-Policy"], "geolocation=(self), camera=(), microphone=()")
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        if method == "HEAD": self.assertEqual(body, b"")
        self.assertIn("frame-ancestors 'none'", lab.CSP)  # No global policy mutation.

    def test_parent_is_not_a_public_host_or_api_origin(self):
        with fixtures.running_server(self.root, public_origin=PUBLIC, frame_parent_origin=PARENT) as (server, _):
            with patch.object(server.unified, "recommend", side_effect=AssertionError("No provider workflow")) as recommend:
                for headers in ({"Host": HOST, "Origin": PARENT},
                                {"Host": "ltybcs.com", "Origin": PARENT},
                                {"Host": HOST, "Origin": "https://ltybcs.com.attacker.test"}):
                    with self.subTest(headers=headers):
                        self.assertEqual(fixtures.request(server, "/", headers=headers)[0], 403)
                        status, reply, _ = fixtures.request(server, "/api/unified/recommend", "POST", b"{}",
                            {**headers, "Content-Type": "application/json"})
                        self.assertEqual(status, 403)
                        self.assertNotIn("Access-Control-Allow-Origin", reply)
                recommend.assert_not_called()
            status, _, raw = fixtures.request(server, "/api/context", "POST",
                json.dumps({"query": "Хочу кофе возле Байтерека на час"}),
                {"Host": HOST, "Origin": PUBLIC, "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["needs_geolocation"])

    def test_invalid_parent_fails_before_catalog_or_runtime_configuration(self):
        invalid = ("", False, "http://ltybcs.com", "https://ltybcs.com/", "https://ltybcs.com/demo",
                   "https://*.ltybcs.com", "https://user:pass@ltybcs.com", "https://ltybcs.com?x=1",
                   "https://ltybcs.com#x", "https://ltybcs.com https://evil.test", "https://ltybcs.com\r\nX-Test: x",
                   "https://ltybcs.com; frame-ancestors *", "https://127.0.0.1", "https://localhost")
        with patch("app.google_lab.load_catalog", side_effect=AssertionError("No catalog read")) as catalog, \
             patch("app.google_lab.GoogleClient.from_runtime", side_effect=AssertionError("No key read")) as google, \
             patch("app.google_lab.UnifiedDGIS", side_effect=AssertionError("No 2GIS configuration")) as dgis, \
             patch("app.google_lab.RouteMap.from_runtime", side_effect=AssertionError("No map configuration")) as maps:
            for value in invalid:
                with self.subTest(parent=value), self.assertRaises(ValueError):
                    lab.create_server(port=0, root=self.root, public_origin=PUBLIC, frame_parent_origin=value,
                                      google_config="DO_NOT_READ", dgis_root="DO_NOT_READ", map_config="DO_NOT_READ")
            for operation in (catalog, google, dgis, maps): operation.assert_not_called()

    def test_cli_passes_explicit_parent_without_widening_bind_or_public_origin(self):
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        server.server_port = 8003
        server.catalog = {"summary": {"visible_count": 1}}
        with patch("app.google_lab.create_server", return_value=server) as create, patch("builtins.print"):
            self.assertEqual(lab.main(["--public-origin", PUBLIC, "--frame-parent-origin", PARENT]), 0)
        self.assertEqual(create.call_args.args, ("127.0.0.1", 8003))
        self.assertEqual(create.call_args.kwargs["public_origin"], PUBLIC)
        self.assertEqual(create.call_args.kwargs["frame_parent_origin"], PARENT)


if __name__ == "__main__":
    unittest.main()
