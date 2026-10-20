"""Offline unified PWA checks; no browser/provider traffic."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

class UnifiedPWATests(unittest.TestCase):
    def test_offline_worker_and_install(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; offline PWA fixture not run")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, str(root / "tests/test_google_lab_pwa.js")],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(report["total"], 20)
        self.assertEqual(report["passed"], report["total"])
        self.assertEqual(report["network_calls"], 0)

    def test_precached_shell_routes_exist(self):
        from app.google_lab import STATIC_FILES
        root = Path(__file__).resolve().parents[1]
        paths = ["/", "/lab.js", "/lab.css", "/pwa.js", "/manifest.webmanifest", "/favicon.svg",
                 "/icons/icon-192.png", "/icons/icon-512.png", "/icons/maskable-512.png",
                 "/icons/apple-touch-icon.png"]
        for route in paths:
            with self.subTest(route=route):
                self.assertIn(route, STATIC_FILES)
                self.assertTrue((root / STATIC_FILES[route][0]).is_file())

if __name__ == "__main__":
    unittest.main()
