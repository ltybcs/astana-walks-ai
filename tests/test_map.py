"""Map privacy/degraded behavior and integrity of vendored official assets."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MapTests(unittest.TestCase):
    def test_vendored_leaflet_matches_manifest_and_official_sri(self):
        vendor = ROOT / "web/vendor/leaflet/1.9.4"
        manifest = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["revision"], "d15112c9e8ac339f0f74f563959d0423d291308d")
        for name, expected in manifest["files"].items():
            with self.subTest(name=name):
                data = (vendor / name).read_bytes()
                self.assertEqual(len(data), expected["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected["sha256"])
        for name, expected in {
            "leaflet.js": "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
            "leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
        }.items():
            digest = hashlib.sha256((vendor / name).read_bytes()).digest()
            self.assertEqual(base64.b64encode(digest).decode("ascii"), expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js optional; map also checked in browser")
    def test_map_requires_consent_for_personal_viewport_and_survives_tile_failure(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests/map_check.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
