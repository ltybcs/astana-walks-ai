"""Isolated lab regressions; no model, Google, 2GIS or Overpass requests."""
from contextlib import contextmanager, redirect_stderr
import hashlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from app import google_lab as lab


@contextmanager
def running_server(root):
    with lab.create_server(port=0, root=root) as server:
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
            self.assertEqual(headers["Permissions-Policy"], "geolocation=(), camera=(), microphone=()")
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
                    self.assertEqual(status, 405)
            for headers in ({"Origin": "https://foreign.example"}, {"Host": "foreign.example"}, {"Host": "127.0.0.1:1"}):
                with self.subTest(headers=headers):
                    self.assertEqual(request(server, "/api/catalog", headers=headers)[0], 403)

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


if __name__ == "__main__":
    unittest.main()
