"""Offline persistence tests, with synthetic public Google-shaped fixtures only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from app.google_cache import GoogleCache, GoogleCacheError, PURPOSE, RETENTION

ANSWER = {"text": "  Whole accepted fixture.\nKeep exact whitespace: ё.  ",
          "selected_place_ids": ["public_id_1"], "source_ids": ["g0"]}
SOURCES = [{"id": "public_id_1", "place": "places/public_id_1", "source_id": "g0",
            "title": "Exact displayed title", "url": "https://www.google.com/maps/place/fixture",
            "attribution": {"title": "Exact attribution", "url": "https://maps.google.com/fixture"},
            "location": {"latitude": 51.1, "longitude": 71.4},
            "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/place/fixture",
                               "directionsUrl": "DO_NOT_KEEP_EXTRA_URL"},
            "unused_metadata": "DO_NOT_KEEP_EXTRA_METADATA"}]


class GoogleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        self.cache = GoogleCache(self.directory, clock=lambda: self.now)
        self.addCleanup(self.cache.close)
        self.path = self.directory / "cache.json"

    def read(self):
        return json.loads(self.path.read_bytes())

    def record_both(self):
        self.cache.record_places({"id": "public_id_1", "location": {"latitude": 51.1, "longitude": 71.4}})
        self.cache.record_grounded(deepcopy(ANSWER), deepcopy(SOURCES))

    def test_places_keeps_ids_and_actual_places_coordinates_only(self):
        data = {"id": "public_id_1", "displayName": {"text": "FORBIDDEN_NAME"},
                "formattedAddress": "FORBIDDEN_ADDRESS", "reviews": [{"text": "FORBIDDEN_REVIEW"}],
                "regularOpeningHours": {"weekdayDescriptions": ["FORBIDDEN_HOURS"]},
                "rating": 4.5, "location": {"latitude": 51.1, "longitude": 71.4}}
        original = deepcopy(data)
        status = self.cache.record_places(data)
        self.assertEqual(data, original)
        self.assertEqual((status["state"], status["places_count"], status["grounded_count"]), ("ready", 1, 0))
        record = self.read()["places"]["public_id_1"]
        self.assertEqual(set(record), {"first_seen", "last_seen", "location"})
        self.assertEqual(record["location"]["provenance"], "places_api_new")
        self.assertEqual(record["location"]["latitude"], 51.1)
        self.assertEqual(datetime.fromisoformat(record["location"]["expires_at"]) - self.now, RETENTION)
        raw = self.path.read_text(encoding="utf-8")
        for forbidden in ("FORBIDDEN", "displayName", "reviews", "OpeningHours", "rating"):
            self.assertNotIn(forbidden, raw)

    def test_gml_coordinates_cannot_enter_places_provenance_path(self):
        before = self.path.read_bytes() if self.path.exists() else None
        for value in (SOURCES[0], {"id": "public_id_1", "source": "gml", "location": {"latitude": 51, "longitude": 71}},
                      {"id": "public_id_1", "location": {"latitude": True, "longitude": 71}},
                      {"id": "public_id_1", "location": {"latitude": float("nan"), "longitude": 71}},
                      {"id": "public_id_1", "location": {"lat": 51, "lon": 71}},
                      {"id": "places/public_id_1"}, {"id": "public_id_1", "query": "FORBIDDEN_QUERY"}):
            with self.assertRaises(GoogleCacheError):
                self.cache.record_places(deepcopy(value))
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_whole_answer_and_minimal_exact_previews_not_raw_provider_data(self):
        original = deepcopy((ANSWER, SOURCES))
        self.cache.record_grounded(deepcopy(ANSWER), deepcopy(SOURCES))
        record = next(iter(self.read()["grounded"].values()))
        self.assertEqual(record["answer"], ANSWER)
        self.assertEqual(record["purpose"], PURPOSE)
        self.assertEqual(record["sources"], [{"id": "public_id_1", "source_id": "g0",
            "title": "Exact displayed title", "url": "https://www.google.com/maps/place/fixture",
            "attribution": {"title": "Exact attribution", "url": "https://maps.google.com/fixture"}}])
        self.assertEqual((ANSWER, SOURCES), original)
        raw = self.path.read_text(encoding="utf-8")
        for forbidden in ("latitude", "longitude", "googleMapsLinks", "DO_NOT_KEEP", "summary", "prompt", "query", "selection_token"):
            self.assertNotIn(forbidden, raw)

    def test_repeated_output_is_immutable_and_does_not_extend_ttl(self):
        self.cache.record_grounded(deepcopy(ANSWER), deepcopy(SOURCES))
        before = self.path.read_bytes()
        self.now += timedelta(days=10)
        result = self.cache.record_grounded(deepcopy(ANSWER), deepcopy(SOURCES))
        self.assertFalse(result["stored"])
        self.assertEqual(self.path.read_bytes(), before)
        changed = deepcopy(ANSWER)
        changed["text"] += "\nA separate new whole accepted output."
        self.cache.record_grounded(changed, deepcopy(SOURCES))
        self.assertEqual(len(self.read()["grounded"]), 2)
        self.assertIn(ANSWER["text"], [r["answer"]["text"] for r in self.read()["grounded"].values()])

    def test_startup_physically_prunes_output_and_locations_at_boundary_keeps_ids(self):
        self.record_both()
        self.now += RETENTION - timedelta(seconds=1)
        self.assertEqual(self.cache.prune()["removed"], 0)
        self.now += timedelta(seconds=1)
        fresh = GoogleCache(self.directory, clock=lambda: self.now)
        self.addCleanup(fresh.close)
        self.assertEqual(self.read()["grounded"], {})
        self.assertEqual(set(self.read()["places"]["public_id_1"]), {"first_seen", "last_seen"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("Whole accepted", raw)
        self.assertNotIn("latitude", raw)
        self.now += timedelta(days=400)
        self.assertEqual(fresh.prune()["places_count"], 1)

    def test_record_access_physically_prunes_even_when_new_input_is_invalid(self):
        self.record_both()
        self.now += RETENTION
        with self.assertRaises(GoogleCacheError):
            self.cache.record_grounded(None, deepcopy(SOURCES))  # A fallback is not an accepted output.
        value = self.read()
        self.assertEqual(value["grounded"], {})
        self.assertNotIn("location", value["places"]["public_id_1"])

    def test_fresh_places_details_may_refresh_location_but_id_first_seen_is_stable(self):
        self.cache.record_places({"id": "public_id_1", "location": {"latitude": 51.1, "longitude": 71.4}})
        first = self.read()["places"]["public_id_1"]["first_seen"]
        self.now += timedelta(days=10)
        self.cache.record_places({"id": "public_id_1"})  # ID-only detail does not renew old coordinates.
        old_expiry = self.read()["places"]["public_id_1"]["location"]["expires_at"]
        self.cache.record_places({"id": "public_id_1", "location": {"latitude": 51.2, "longitude": 71.5}})
        record = self.read()["places"]["public_id_1"]
        self.assertEqual(record["first_seen"], first)
        self.assertNotEqual(record["last_seen"], first)
        self.assertGreater(record["location"]["expires_at"], old_expiry)
        self.assertEqual(record["location"]["latitude"], 51.2)

    def test_fallback_unassociated_and_extra_answer_fields_are_rejected(self):
        for answer in (None, {"summary": "raw fallback"}, {**ANSWER, "prompt": "FORBIDDEN_PROMPT"},
                       {**ANSWER, "selected_place_ids": ["other_id"]},
                       {**ANSWER, "source_ids": ["g1"]}, {**ANSWER, "text": ""}):
            with self.assertRaises(GoogleCacheError):
                self.cache.record_grounded(deepcopy(answer), deepcopy(SOURCES))
        self.assertFalse(self.path.exists())

    def test_corruption_duplicate_fields_invalid_ttl_and_extra_content_fail_closed(self):
        self.record_both()
        baseline = self.read()
        bad_values = [{"version": 1, "places": {}, "grounded": {}, "reviews": ["FORBIDDEN"]}]
        extra = deepcopy(baseline)
        next(iter(extra["grounded"].values()))["sources"][0]["location"] = {"latitude": 51, "longitude": 71}
        bad_values.append(extra)
        extended = deepcopy(baseline)
        record = next(iter(extended["grounded"].values()))
        record["expires_at"] = (self.now + timedelta(days=31)).isoformat()
        bad_values.append(extended)
        changed = deepcopy(baseline)
        next(iter(changed["grounded"].values()))["answer"]["text"] += "tampered"
        bad_values.append(changed)
        raws = [b"invalid", b'{"version":1,"version":1,"places":{},"grounded":{}}', b'{"version":NaN}']
        raws += [json.dumps(value, ensure_ascii=False).encode("utf-8") for value in bad_values]
        for raw in raws:
            self.path.write_bytes(raw)
            with self.assertRaises(GoogleCacheError) as caught:
                GoogleCache(self.directory, clock=lambda: self.now)
            self.assertEqual(caught.exception.code, "google_cache_unavailable")
            self.assertEqual(self.path.read_bytes(), raw)

    def test_atomic_failure_preserves_old_snapshot_cleans_temp_and_sanitizes_error(self):
        self.cache.record_places({"id": "public_id_1"})
        before = self.path.read_bytes()
        sensitive = "FAKE_SECRET C:/private-runtime/key"
        with patch("app.google_cache.os.replace", side_effect=OSError(sensitive)):
            with self.assertRaises(GoogleCacheError) as caught:
                self.cache.record_grounded(deepcopy(ANSWER), deepcopy(SOURCES))
        self.assertNotIn(sensitive, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob(".cache-*.tmp")), [])
        self.assertEqual(self.cache.prune()["places_count"], 1)

    def test_crash_orphans_removed_eagerly_without_reading_or_deleting_foreign_files(self):
        orphan = self.directory / ".cache-abcdefgh.tmp"
        with os.fdopen(os.open(orphan, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
            output.write(b"STALE_GOOGLE_CONTENT")
        foreign = self.directory / "operator-notes.txt"
        foreign.write_bytes(b"do not touch")
        status = self.cache.prune()
        self.assertEqual(status["removed_orphans"], 1)
        self.assertFalse(orphan.exists())
        self.assertEqual(foreign.read_bytes(), b"do not touch")

    def test_absolute_existing_directory_and_regular_private_file_required(self):
        for path in ("relative-cache", self.directory / "missing"):
            with self.assertRaises(GoogleCacheError):
                GoogleCache(path)
        self.cache.record_places({"id": "public_id_1"})
        if os.name != "nt":
            self.path.chmod(0o644)
            with self.assertRaises(GoogleCacheError):
                self.cache.prune()
            self.path.chmod(0o600)
        with tempfile.NamedTemporaryFile(dir=self.directory) as output:
            with self.assertRaises(GoogleCacheError):
                GoogleCache(Path(output.name))

    def test_hardlinks_are_refused_without_touching_target(self):
        target = self.directory / "private-target"
        target.write_bytes(b"private target bytes")
        try:
            os.link(target, self.path)
        except OSError as error:
            self.skipTest("Hardlinks unavailable: " + type(error).__name__)
        with self.assertRaises(GoogleCacheError):
            self.cache.prune()
        self.assertEqual(target.read_bytes(), b"private target bytes")

    def test_symlink_cache_directory_file_lock_and_orphan_are_refused(self):
        target = self.directory / "private-target"
        target.write_bytes(b"private target bytes")
        probe = self.directory / "probe"
        try:
            probe.symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks requires Windows privilege")
        probe.unlink()
        for candidate in (self.path, self.directory / ".cache-abcdefgh.tmp", self.directory / "cache.lock"):
            if candidate.exists():
                candidate.unlink()
            candidate.symlink_to(target)
            try:
                with self.assertRaises(GoogleCacheError):
                    self.cache.prune()
                self.assertEqual(target.read_bytes(), b"private target bytes")
            finally:
                candidate.unlink()
        alias = self.directory / "directory-alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(GoogleCacheError):
            GoogleCache(alias)

    def test_threads_and_processes_share_atomic_storage_without_lost_records(self):
        def record(index):
            return GoogleCache(self.directory).record_places({"id": "thread_" + str(index)})
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertTrue(all(status["stored"] for status in executor.map(record, range(16))))
        script = "from app.google_cache import GoogleCache;import sys;c=GoogleCache(sys.argv[1]);[c.record_places({'id':sys.argv[2]+str(i)}) for i in range(5)];c.close()"
        children = []
        try:
            for prefix in ("process_a_", "process_b_"):
                children.append(subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", script, str(self.directory), prefix],
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
        self.assertEqual(len(self.read()["places"]), 26)

    def test_running_app_cleanup_physically_prunes_while_idle_and_close_stops_worker(self):
        self.record_both()
        self.cache.start_cleanup(interval_seconds=0.01)
        self.now += RETENTION
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if not self.read()["grounded"] and "location" not in self.read()["places"]["public_id_1"]:
                break
            sleep(0.01)
        self.assertEqual(self.read()["grounded"], {})
        self.assertNotIn("location", self.read()["places"]["public_id_1"])
        self.assertLess(RETENTION + timedelta(hours=1), timedelta(days=30))
        self.cache.close()
        self.assertFalse(self.cache._worker.is_alive())

    def test_status_is_sanitized_and_reports_background_or_current_failure(self):
        self.assertEqual(self.cache.status(), {"enabled": True, "error": None})
        self.cache.record_places({"id": "public_id_1"})
        self.path.write_bytes(b"private corrupt bytes")
        self.assertEqual(self.cache.status(), {"enabled": True, "error": "google_cache_unavailable"})
        self.assertEqual(self.path.read_bytes(), b"private corrupt bytes")
        self.cache.start_cleanup(interval_seconds=0.01)
        deadline = monotonic() + 2
        while self.cache.cleanup_error is None and monotonic() < deadline:
            sleep(0.01)
        self.assertEqual(self.cache.cleanup_error, "google_cache_unavailable")
        self.cache.close()


if __name__ == "__main__":
    unittest.main()
