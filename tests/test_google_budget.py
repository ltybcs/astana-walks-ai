"""Offline ledger tests: real durable counters, fake operation names only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from app.google_budget import GoogleBudget, GoogleError


class GoogleBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "pilot.json"

    def test_missing_and_corrupt_never_create_or_reset_grant(self):
        budget = GoogleBudget(self.path)
        self.assertEqual(budget.status()["state"], "unavailable")
        with self.assertRaises(GoogleError) as caught:
            budget.reserve("mcp.initialize")
        self.assertEqual(caught.exception.code, "quota_unavailable")
        self.assertFalse(self.path.exists())
        for raw in (b"{}", b'{"used":NaN}', b"invalid", b"x" * 16385):
            self.path.write_bytes(raw)
            with self.assertRaises(GoogleError):
                budget.reserve("places.hours")
            self.assertEqual(self.path.read_bytes(), raw)

    def test_init_is_explicit_exclusive_and_all_500_are_durable(self):
        GoogleBudget.initialize(self.path)
        with self.assertRaises(FileExistsError):
            GoogleBudget.initialize(self.path)
        budget = GoogleBudget(self.path)
        for _ in range(499):
            budget.reserve("mcp.initialized")
        self.assertEqual(GoogleBudget(self.path).reserve("places.reviews"), {"used": 500, "limit": 500, "remaining": 0, "state": "exhausted"})
        with self.assertRaises(GoogleError) as caught:
            budget.reserve("mcp.initialize")
        self.assertEqual((caught.exception.code, caught.exception.status), ("quota_exhausted", 429))
        state = json.loads(self.path.read_bytes())
        self.assertEqual(state["operations"], {"mcp.initialized": 499, "places.reviews": 1})
        self.assertEqual(set(state), {"version", "limit", "used", "operations", "initialized_at", "updated_at"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_threads_and_separate_processes_share_one_counter(self):
        GoogleBudget.initialize(self.path)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: GoogleBudget(self.path).reserve("places.hours")["used"], range(32)))
        self.assertEqual(sorted(results), list(range(1, 33)))
        script = "from app.google_budget import GoogleBudget;import sys;b=GoogleBudget(sys.argv[1]);[b.reserve('mcp.tools_list') for _ in range(12)]"
        children = []
        try:
            for _ in range(2):
                children.append(subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", script, str(self.path)],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            for child in children:
                stdout, stderr = child.communicate(timeout=20)
                self.assertEqual((child.returncode, stdout, stderr), (0, b"", b""))
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)
        self.assertEqual(GoogleBudget(self.path).status()["used"], 56)

    def test_invalid_counts_duplicates_and_wrong_limit_fail_closed(self):
        GoogleBudget.initialize(self.path)
        original = json.loads(self.path.read_bytes())
        for changes in ({"used": True}, {"used": 1}, {"limit": 501}, {"operations": {"other": 0}}, {"used": 501}):
            self.path.write_text(json.dumps({**original, **changes}), encoding="utf-8")
            with self.assertRaises(GoogleError):
                GoogleBudget(self.path).reserve("places.hours")

    def test_relative_path_and_symlink_are_not_a_shared_ledger(self):
        with self.assertRaises(ValueError):
            GoogleBudget("relative-ledger.json")
        target = self.path.parent / "target"
        target.write_bytes(b"private")
        try:
            self.path.symlink_to(target)
        except OSError:
            return  # Other path checks are meaningful even without Windows privilege.
        with self.assertRaises(ValueError):
            GoogleBudget(self.path)


if __name__ == "__main__":
    unittest.main()
