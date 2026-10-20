"""Offline ledger tests: real durable counters, fake operation names only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import errno
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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


    @staticmethod
    def sharing_error(code):
        error = PermissionError(errno.EACCES, "PRIVATE_IO_ERROR")
        error.winerror = code
        return error

    def test_windows_reader_collisions_retry_same_fsynced_snapshot_then_one_http_attempt(self):
        from app.google_client import GoogleClient
        GoogleBudget.initialize(self.path)
        budget = GoogleBudget(self.path)
        original = self.path.read_bytes()
        real_replace, real_fsync = os.replace, os.fsync
        elapsed, attempts, snapshots = [0.0], [], []
        errors = iter((5, 32, 33))
        def replace(source, target):
            attempts.append((source, target))
            snapshots.append(Path(source).read_bytes())
            self.assertEqual(self.path.read_bytes(), original)
            try:
                code = next(errors)
            except StopIteration:
                real_replace(source, target)
            else:
                raise self.sharing_error(code)
        def sleep(seconds):
            elapsed[0] += seconds
        transport = Mock(return_value=object())
        client = GoogleClient("DUMMY_NOT_A_KEY", budget, http_open=transport)
        with patch("app.google_budget.os.replace", side_effect=replace) as replaced, \
             patch("app.google_budget.os.fsync", wraps=real_fsync) as fsynced, \
             patch("app.google_budget.monotonic", side_effect=lambda: elapsed[0]), \
             patch("app.google_budget.sleep", side_effect=sleep):
            client._http("GET", "https://places.googleapis.com/v1/places/synthetic", "places.hours")
        self.assertEqual(replaced.call_count, 4)
        self.assertEqual(fsynced.call_count, 1)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(len(set(snapshots)), 1)
        self.assertEqual(json.loads(snapshots[0])["used"], 1)
        self.assertEqual(transport.call_count, 1)
        self.assertAlmostEqual(elapsed[0], .03)
        self.assertEqual(budget.status()["used"], 1)
        self.assertEqual(json.loads(self.path.read_bytes())["operations"], {"places.hours": 1})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_windows_sharing_deadline_keeps_durable_counter_and_prevents_http(self):
        from app.google_client import GoogleClient
        GoogleBudget.initialize(self.path)
        budget = GoogleBudget(self.path)
        original = self.path.read_bytes()
        elapsed, snapshots = [0.0], []
        def replace(source, target):
            snapshots.append((source, Path(source).read_bytes()))
            raise self.sharing_error(5)
        def sleep(seconds):
            elapsed[0] += seconds
        transport = Mock(side_effect=AssertionError("Reservation must succeed first"))
        client = GoogleClient("DUMMY_NOT_A_KEY", budget, http_open=transport)
        with patch("app.google_budget.os.replace", side_effect=replace) as replaced, \
             patch("app.google_budget.monotonic", side_effect=lambda: elapsed[0]), \
             patch("app.google_budget.sleep", side_effect=sleep):
            with self.assertRaises(GoogleError) as caught:
                client._http("GET", "https://places.googleapis.com/v1/places/synthetic", "places.hours")
        self.assertEqual(caught.exception.code, "quota_unavailable")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertAlmostEqual(elapsed[0], .2)
        self.assertGreater(replaced.call_count, 1)
        self.assertLessEqual(replaced.call_count, 21)
        self.assertEqual(len(set(snapshots)), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(budget.status()["used"], 0)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        transport.assert_not_called()

    def test_other_permission_or_io_errors_fail_closed_without_retry(self):
        GoogleBudget.initialize(self.path)
        budget = GoogleBudget(self.path)
        original = self.path.read_bytes()
        for error in (PermissionError(errno.EACCES, "PRIVATE_POSIX_PERMISSION"),
                      self.sharing_error(2), OSError(errno.ENOSPC, "PRIVATE_DISK_FULL")):
            with self.subTest(winerror=getattr(error, "winerror", None)), \
                 patch("app.google_budget.os.replace", side_effect=error) as replaced, \
                 patch("app.google_budget.sleep") as sleep:
                with self.assertRaises(GoogleError) as caught:
                    budget.reserve("places.hours")
            self.assertEqual(caught.exception.code, "quota_unavailable")
            self.assertNotIn("PRIVATE", str(caught.exception))
            replaced.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


    def seeded(self, used=400, limit=500):
        GoogleBudget.initialize(self.path)
        value = json.loads(self.path.read_bytes())
        value.update(limit=limit, used=used, operations={"places.resolve": used})
        self.path.write_bytes(json.dumps(value, separators=(",", ":")).encode())
        return value

    def test_explicit_raise_preserves_usage_operations_and_initialized_stamp(self):
        before = self.seeded()
        before["operations"] = {"mcp.search_places": 250, "mcp.compute_routes": 130, "places.resolve": 20}
        self.path.write_bytes(json.dumps(before).encode())
        with patch("app.google_budget.utc_now", return_value="2026-10-21T12:00:00+00:00") as clock:
            status = GoogleBudget(self.path).raise_to_750()
        self.assertEqual(status, {"used": 400, "limit": 750, "remaining": 350, "state": "ready"})
        self.assertEqual(json.loads(self.path.read_bytes()),
                         {**before, "limit": 750, "updated_at": "2026-10-21T12:00:00+00:00"})
        clock.assert_called_once_with()
        self.assertEqual(GoogleBudget(self.path).status(), status)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_750_raise_is_idempotent_without_timestamp_write_or_charge(self):
        self.seeded(limit=750)
        before = self.path.read_bytes()
        with patch("app.google_budget.utc_now", side_effect=AssertionError("Do not rewrite time")), \
             patch.object(GoogleBudget, "_write_atomic", side_effect=AssertionError("Do not rewrite ledger")):
            self.assertEqual(GoogleBudget(self.path).raise_to_750(),
                             {"used": 400, "limit": 750, "remaining": 350, "state": "ready"})
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(TypeError):
            GoogleBudget(self.path).raise_to_750(500)

    def test_reservation_uses_stored_ceiling_and_never_raises_it_automatically(self):
        before = self.seeded(used=500)
        budget = GoogleBudget(self.path)
        with self.assertRaises(GoogleError) as caught:
            budget.reserve("places.hours")
        self.assertEqual(caught.exception.code, "quota_exhausted")
        self.assertEqual(json.loads(self.path.read_bytes()), before)
        budget.raise_to_750()
        value = json.loads(self.path.read_bytes())
        value.update(used=749, operations={"places.resolve": 749})
        self.path.write_bytes(json.dumps(value).encode())
        self.assertEqual(budget.reserve("places.hours"),
                         {"used": 750, "limit": 750, "remaining": 0, "state": "exhausted"})
        exhausted = self.path.read_bytes()
        with self.assertRaises(GoogleError) as caught:
            GoogleBudget(self.path).reserve("mcp.initialize")
        self.assertEqual((caught.exception.code, caught.exception.status), ("quota_exhausted", 429))
        self.assertIn("750", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), exhausted)

    def test_raise_missing_corrupt_unapproved_and_overused_fail_without_creation_or_reset(self):
        with self.assertRaises(GoogleError):
            GoogleBudget(self.path).raise_to_750()
        self.assertEqual(list(self.path.parent.iterdir()), [])
        valid = self.seeded()
        invalid = [b"{}", b"invalid", b"x" * 16385]
        invalid += [json.dumps({**valid, "limit": limit}).encode()
                    for limit in (True, 0, 400, 499, 501, 749, 751, 1000)]
        invalid += [json.dumps({**valid, "limit": 750, "used": 751, "operations": {"places.resolve": 751}}).encode(),
                    json.dumps({**valid, "used": 401}).encode()]
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                self.path.write_bytes(raw)
                with self.assertRaises(GoogleError) as caught:
                    GoogleBudget(self.path).raise_to_750()
                self.assertEqual(caught.exception.code, "quota_unavailable")
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_raise_atomic_write_failure_keeps_original_grant_and_cleans_snapshot(self):
        self.seeded()
        before = self.path.read_bytes()
        with patch("app.google_budget.os.replace", side_effect=OSError(errno.ENOSPC, "PRIVATE_DISK_ERROR")) as replaced:
            with self.assertRaises(GoogleError) as caught:
                GoogleBudget(self.path).raise_to_750()
        self.assertEqual(caught.exception.code, "quota_unavailable")
        self.assertNotIn("PRIVATE", str(caught.exception))
        replaced.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(GoogleBudget(self.path).status()["used"], 400)
        self.assertEqual(GoogleBudget(self.path).status()["limit"], 500)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_raise_rereads_latest_usage_after_taking_the_shared_lock(self):
        from app import google_budget
        before = self.seeded()
        actual_lock = google_budget._locked
        advanced = False
        @contextmanager
        def concurrent_writer(path, timeout=5):
            nonlocal advanced
            if not advanced:
                advanced = True
                # Represents another valid writer completing after precheck and
                # before migration gets the same real OS lock. No stale reset.
                GoogleBudget(path).reserve("places.hours")
            with actual_lock(path, timeout=timeout):
                yield
        with patch("app.google_budget._locked", side_effect=concurrent_writer):
            status = GoogleBudget(self.path).raise_to_750()
        state = json.loads(self.path.read_bytes())
        self.assertEqual(status["used"], 401)
        self.assertEqual(state["limit"], 750)
        self.assertEqual(state["operations"], {"places.resolve": 400, "places.hours": 1})
        self.assertEqual(state["initialized_at"], before["initialized_at"])

    def test_explicit_cli_requires_exact_action_and_existing_absolute_path(self):
        from app.google_budget import main
        self.seeded()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--raise-to-750", str(self.path)]), 0)
            before = self.path.read_bytes()
            self.assertEqual(main(["--raise-to-750", str(self.path)]), 0)
            self.assertEqual(self.path.read_bytes(), before)
            for arguments in (["--raise-to-1000", str(self.path)], ["--raise-to-75", str(self.path)],
                              ["--init", str(self.path), "--raise-to-750", str(self.path)],
                              ["--raise-to-750", "relative.json"], ["--init", str(self.path)]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    main(arguments)
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
