"""Run the frontend race/privacy tests from unittest discovery when Node is available."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

class GoogleLabFrontendTests(unittest.TestCase):
    def test_offline_frontend_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable; frontend DOM fixture suite not run")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, str(root / "tests/test_google_lab_frontend.js")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(report["total"], 20)
        self.assertEqual(report["passed"], report["total"])
        self.assertEqual(report["network_calls"], 0)

if __name__ == "__main__":
    unittest.main()
