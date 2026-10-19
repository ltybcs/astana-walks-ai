"""Offline refresh tests: last-good retention, daily TTL and atomic index swap."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from app.data_refresh import COOLDOWN_SECONDS, TTL_SECONDS, DatasetStore, POIRefresher
from app.retrieval import Retriever
from scripts.collect_osm import ASTANA_BBOX, QUERY_PROFILE, build_query, collect_response, make_document, parse_json


def payload(name="Новый парк"):
    return {"osm3s": {"timestamp_osm_base": "2026-10-19T00:00:00Z"}, "elements": [{"type": "node", "id": 7, "lat": 51.1, "lon": 71.4, "timestamp": "2026-10-18T00:00:00Z", "tags": {"name": name, "leisure": "park"}}]}


def index(collected):
    return Retriever(data=make_document(payload("Старый парк"), datetime.fromtimestamp(collected, timezone.utc).isoformat()))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.now = [1_800_000_000.0]
        self.clock = lambda: self.now[0]

    def test_fresh_shipped_snapshot_needs_no_state_and_no_startup_fetch(self):
        fetcher = Mock(side_effect=AssertionError("Network must not run"))
        refresh = POIRefresher(DatasetStore(index(self.now[0] - 100)), self.cache, fetcher=fetcher, clock=self.clock)
        self.assertFalse(refresh.refresh_once())
        fetcher.assert_not_called()
        self.assertEqual(refresh.status()["state"], "fresh")
        self.assertEqual(refresh.status()["ttl_hours"], 24)
        self.assertIsNone(refresh.status()["last_attempt_at"])
        refresh.start()
        self.assertTrue(refresh.stop())
        fetcher.assert_not_called()

    def test_success_keeps_source_bytes_and_publishes_one_completed_new_index(self):
        original = index(self.now[0] - TTL_SECONDS - 1)
        store = DatasetStore(original)
        source = payload()
        raw = json.dumps(source, separators=(",", ":")).encode()
        fetcher = Mock(return_value=(source, raw))
        refresh = POIRefresher(store, self.cache, fetcher=fetcher, clock=self.clock)
        self.assertTrue(refresh.refresh_once())
        self.assertEqual(original.places[0]["name"], "Старый парк")
        self.assertEqual(store.snapshot().places[0]["name"], "Новый парк")
        self.assertEqual(refresh.status()["state"], "updated")
        self.assertEqual(len(list((self.cache / "snapshots").glob("*.overpass.json"))), 1)
        self.assertEqual(next((self.cache / "snapshots").glob("*.overpass.json")).read_bytes(), raw)
        self.assertFalse(refresh.refresh_once(force=True))
        self.assertEqual(fetcher.call_count, 1)

    def test_failure_retains_last_good_file_index_and_cooldown_across_restart(self):
        source = payload()
        fetcher = Mock(return_value=(source, json.dumps(source).encode()))
        store = DatasetStore(index(self.now[0] - TTL_SECONDS - 1))
        refresh = POIRefresher(store, self.cache, fetcher=fetcher, clock=self.clock)
        self.assertTrue(refresh.refresh_once())
        last_good = (self.cache / "poi.astana.json").read_bytes()
        previous_index = store.snapshot()
        self.now[0] += TTL_SECONDS + 1
        fetcher.side_effect = TimeoutError("Sensitive provider detail must not surface")
        self.assertFalse(refresh.refresh_once())
        self.assertEqual((self.cache / "poi.astana.json").read_bytes(), last_good)
        self.assertIs(store.snapshot(), previous_index)
        self.assertEqual(refresh.status()["last_error"], "network_timeout")
        next_fetcher = Mock(side_effect=AssertionError("Persistent cooldown must prevent retry"))
        restarted = POIRefresher(DatasetStore(index(self.now[0] - 2 * TTL_SECONDS)), self.cache, fetcher=next_fetcher, clock=self.clock)
        self.assertFalse(restarted.refresh_once(force=True))
        next_fetcher.assert_not_called()
        self.assertNotIn("Sensitive", json.dumps(refresh.status()))

    def test_partial_or_outside_geometry_does_not_replace_good_data(self):
        for bad in ({"remark": "partial", "elements": []}, {"elements": [{"type": "node", "id": 7, "lat": 0, "lon": 0, "tags": {"name": "Outside", "leisure": "park"}}]}):
            with self.subTest(payload=bad), tempfile.TemporaryDirectory() as folder:
                original = index(self.now[0] - TTL_SECONDS - 1)
                store = DatasetStore(original)
                refresh = POIRefresher(store, folder, fetcher=Mock(return_value=(bad, json.dumps(bad).encode())), clock=self.clock)
                self.assertFalse(refresh.refresh_once())
                self.assertIs(store.snapshot(), original)
                self.assertFalse((Path(folder) / "poi.astana.json").exists())

    def test_corrupt_cache_or_newer_shipped_snapshot_wins_without_network(self):
        (self.cache / "poi.astana.json").write_bytes(b"not-json")
        original = index(self.now[0] - 100)
        refresh = POIRefresher(DatasetStore(original), self.cache, clock=self.clock)
        self.assertFalse(refresh.load_cache())
        self.assertEqual(refresh.status()["last_error"], "invalid_cache")
        old = make_document(payload(), datetime.fromtimestamp(self.now[0] - 200, timezone.utc).isoformat())
        (self.cache / "poi.astana.json").write_bytes(json.dumps(old).encode())
        self.assertFalse(refresh.load_cache())
        self.assertIs(refresh.store.snapshot(), original)

    def test_success_state_cannot_make_a_corrupt_cache_hide_stale_served_data(self):
        original = index(self.now[0] - TTL_SECONDS - 100)
        (self.cache / "poi.astana.json").write_bytes(b"not-json")
        (self.cache / "refresh-state.json").write_bytes(json.dumps({"last_success": self.now[0] - 100, "last_attempt": self.now[0] - 2 * COOLDOWN_SECONDS}).encode())
        fetcher = Mock(side_effect=TimeoutError())
        refresh = POIRefresher(DatasetStore(original), self.cache, fetcher=fetcher, clock=self.clock)
        self.assertFalse(refresh.refresh_once())
        fetcher.assert_called_once()
        self.assertTrue(refresh.status()["stale"])
        self.assertIs(refresh.store.snapshot(), original)

    def test_corrupt_status_cannot_expose_arbitrary_error_text(self):
        (self.cache / "refresh-state.json").write_bytes(json.dumps({"last_attempt": self.now[0] - 100, "last_error": "https://private.example/query?secret=not-for-status"}).encode())
        fetcher = Mock(side_effect=AssertionError("Corrupt state must cool down"))
        refresh = POIRefresher(DatasetStore(index(self.now[0] - TTL_SECONDS - 1)), self.cache, fetcher=fetcher, clock=self.clock)
        self.assertFalse(refresh.refresh_once())
        fetcher.assert_not_called()
        self.assertEqual(refresh.status()["last_error"], "invalid_refresh_state")
        self.assertNotIn("secret", json.dumps(refresh.status()))

    def test_corrupt_state_repair_is_persisted_once_then_refresh_resumes(self):
        (self.cache / "refresh-state.json").write_bytes(b"broken-json")
        source = payload()
        fetcher = Mock(return_value=(source, json.dumps(source).encode()))
        refresh = POIRefresher(DatasetStore(index(self.now[0] - 2 * TTL_SECONDS)), self.cache, fetcher=fetcher, clock=self.clock)
        self.assertFalse(refresh.refresh_once())
        repaired_at = json.loads((self.cache / "refresh-state.json").read_bytes())["last_attempt"]
        self.now[0] += 60
        self.assertFalse(refresh.refresh_once())
        self.assertEqual(json.loads((self.cache / "refresh-state.json").read_bytes())["last_attempt"], repaired_at)
        self.now[0] = repaired_at + COOLDOWN_SECONDS + 1
        self.assertTrue(refresh.refresh_once())
        fetcher.assert_called_once()

    def test_status_can_describe_the_same_snapshot_as_the_http_response(self):
        previous = index(self.now[0] - TTL_SECONDS - 1)
        store = DatasetStore(previous)
        refresh = POIRefresher(store, self.cache, clock=self.clock)
        store.publish(index(self.now[0]))
        self.assertTrue(refresh.status(snapshot=previous)["stale"])
        self.assertFalse(refresh.status()["stale"])
        self.assertEqual(refresh.status(snapshot=previous)["last_good_at"], previous.dataset["collected_at"])

    def test_nonfinite_source_or_cache_metadata_cannot_break_api_serialization(self):
        source = payload()
        source["osm3s"]["timestamp_osm_base"] = float("nan")
        original = index(self.now[0] - TTL_SECONDS - 1)
        store = DatasetStore(original)
        refresh = POIRefresher(store, self.cache, fetcher=Mock(return_value=(source, json.dumps(source).encode())), clock=self.clock)
        self.assertFalse(refresh.refresh_once())
        self.assertIs(store.snapshot(), original)
        self.assertFalse((self.cache / "poi.astana.json").exists())
        cached = make_document(payload(), datetime.fromtimestamp(self.now[0], timezone.utc).isoformat())
        cached["dataset"]["source_snapshot_at"] = float("nan")
        (self.cache / "poi.astana.json").write_bytes(json.dumps(cached).encode())
        self.assertFalse(refresh.load_cache())
        self.assertIs(store.snapshot(), original)
        json.dumps(store.snapshot().dataset, allow_nan=False)

    def test_source_decoder_rejects_nonfinite_values_and_duplicate_keys(self):
        for raw in (b'{"elements": [], "x": NaN}', b'{"elements": [], "x": Infinity}', b'{"elements": [], "x": 1e999}', b'{"elements": [], "elements": []}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_json(raw)

    def test_multilingual_names_and_unnamed_leisure_are_explicit_source_labels(self):
        data = payload()
        data["elements"][0]["tags"] = {"leisure": "playground"}
        data["elements"].append({"type": "node", "id": 8, "lat": 51.1, "lon": 71.4, "tags": {"amenity": "cafe", "name:kk": "Қазақша атау"}})
        data["elements"].append({"type": "node", "id": 9, "lat": 51.1, "lon": 71.4, "tags": {"amenity": "cafe"}})
        document = make_document(data, "2026-10-19T00:00:00Z")
        generated, named = document["places"]
        self.assertTrue(generated["name_generated"])
        self.assertIsNone(generated["source_name"])
        self.assertNotIn("name", generated["source_tags"])
        self.assertIn("Без названия", generated["name"])
        self.assertEqual(named["source_name"], "Қазақша атау")
        self.assertEqual(document["dataset"]["unnamed_count"], 1)
        self.assertEqual(len(document["places"]), 2)
        query = build_query(ASTANA_BBOX, profile=QUERY_PROFILE)
        self.assertIn('nwr["leisure"="playground"](', query)
        self.assertIn('^name(:ru|:kk|:en)?$', query)

    def test_overpass_rate_limit_closes_error_and_waits_before_single_retry(self):
        denied = HTTPError("https://overpass-api.de/api/interpreter", 429, "limit", {}, io.BytesIO(b"no"))
        response = io.BytesIO(json.dumps(payload()).encode())
        response.headers = {}
        stopped = Mock(spec=Event)
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        with patch("scripts.collect_osm.open_overpass", side_effect=[denied, response]) as opened:
            actual, _ = collect_response("https://overpass-api.de/api/interpreter", ASTANA_BBOX, stop_event=stopped)
        self.assertEqual(actual["elements"][0]["id"], 7)
        self.assertEqual(opened.call_count, 2)
        stopped.wait.assert_called_once_with(30)
        self.assertTrue(denied.fp.closed)


if __name__ == "__main__":
    unittest.main()
