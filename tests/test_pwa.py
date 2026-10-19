"""Installable resources are delivered without exposing repository files."""

import http.client
import json
from pathlib import Path
import shutil
import struct
import subprocess
from threading import Thread
import unittest

from app.server import create_server
from tests.test_retrieval import index, poi


class PWATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, retriever=index(poi("p")))
        cls.thread = Thread(target=cls.server.serve_forever,
                            kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_manifest_and_actual_icons_support_standalone_installation(self):
        status, headers, body = self.request("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/manifest+json")
        manifest = json.loads(body)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["id"], "/")
        self.assertFalse(manifest.get("prefer_related_applications", False))
        sizes = set()
        for icon in manifest["icons"]:
            status, headers, body = self.request(icon["src"])
            self.assertEqual(status, 200, icon["src"])
            self.assertEqual(headers["content-type"], "image/png")
            self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", body[16:24])
            self.assertEqual(icon["sizes"], f"{width}x{height}")
            sizes.add(icon["sizes"])
        self.assertTrue({"192x192", "512x512"}.issubset(sizes))
        status, _, body = self.request("/icons/apple-touch-icon.png")
        self.assertEqual(status, 200)
        self.assertEqual(struct.unpack(">II", body[16:24]), (180, 180))

    def test_worker_scripts_and_head_are_served_but_other_paths_stay_private(self):
        for path in ("/sw.js", "/pwa.js", "/favicon.svg", "/manifest.webmanifest"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(self.request(path, "HEAD")[0], 200)
        for path in ("/icons/", "/icons/../.env", "/icons/unknown.png",
                     "/sw.js/../app/server.py", "/manifest.webmanifest/../../.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
                self.assertEqual(self.request(path, "HEAD")[0], 404)

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional; browser QA also checks the worker")
    def test_worker_network_and_offline_behavior_never_caches_api(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/pwa_worker_check.cjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is optional; browser QA also checks installation")
    def test_native_and_manual_installation_paths_keep_one_honest_label(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/pwa_install_check.cjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
