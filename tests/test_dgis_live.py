"""Offline tests: synthetic cards and dummy keys; never call the provider."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from app import dgis_live as live
from scripts.collect_2gis import FIELDS as COLLECTOR_FIELDS, normalize_items


SECRET = "dummy-sensitive-key-not-a-real-credential"


def card(**updates):
    result = {"id": "70000000000001", "name": "Синтетический парк", "type": "adm_div.place",
              "point": {"lat": 51.1546, "lon": 71.4252}, "rubrics": [{"name": "Парки"}]}
    result.update(updates)
    return result


def envelope(items=None):
    values = [card()] if items is None else items
    return {"meta": {"code": 200}, "result": {"items": values, "total": len(values)}}


class LiveSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.now = 1_790_000_000.0
        self.client = live._LiveClient(self.root, lambda: self.now)
        self.environment = patch.dict(os.environ, {"DGIS_API_KEY": SECRET})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.transport = patch("app.dgis_live.build_opener")
        self.factory = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.factory.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(envelope()).encode())

    def state(self):
        return json.loads((self.client.directory / "usage.json").read_bytes())

    def test_one_fixed_official_request_with_no_user_text_or_location(self):
        result = self.client.fetch("park")
        self.assertEqual(self.factory.return_value.open.call_count, 1)
        args, kwargs = self.factory.return_value.open.call_args
        parsed = urlparse(args[0].full_url)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path),
                         ("https", "catalog.api.2gis.com", "/3.0/items"))
        params = parse_qs(parsed.query)
        self.assertEqual(params, {"key": [SECRET], "q": ["Парки"], "point1": ["71.2,51.32"],
                                  "point2": ["71.7,51.0"], "page": ["1"], "page_size": ["10"],
                                  "fields": ["items.point,items.rubrics"]})
        self.assertEqual(kwargs["timeout"], 25)
        self.assertEqual(result["attribution"], {"text": "2ГИС", "url": "http://dev.2gis.ru"})
        self.assertEqual(result["places"][0]["features"]["socket"], "unknown")
        self.assertEqual(result["quota"]["used"], 1)
        self.assertEqual(result["quota"]["remaining"], live.LIMIT - 1)

    def test_no_cards_queries_or_keys_are_persisted(self):
        self.client.fetch("park")
        names = {p.name for p in self.client.directory.iterdir()}
        self.assertEqual(names, {"usage.json", "usage.lock"})
        for path in self.client.directory.iterdir():
            value = path.read_bytes()
            self.assertNotIn(SECRET.encode(), value)
            self.assertNotIn("Синтетический парк".encode(), value)
            self.assertNotIn(b"places", value)
            self.assertNotIn("Парки".encode(), value)

    def test_invalid_category_rejected_before_key_storage_or_network(self):
        for value in (None, True, [], {}, "park?key=anything", "Парк", "all", ""):
            with self.subTest(value=value), self.assertRaises(live.DGISLiveError) as caught:
                self.client.fetch(value)
            self.assertEqual(caught.exception.http_status, 400)
        self.factory.assert_not_called()
        self.assertFalse(self.client.directory.exists())

    def test_missing_key_does_not_seed_or_charge_quota(self):
        with patch.dict(os.environ, {"DGIS_API_KEY": ""}):
            self.assertEqual(self.client.status(), {"configured": False, "available": False,
                                                   "quota": None, "code": "not_configured"})
            with self.assertRaises(live.DGISLiveError) as caught:
                self.client.fetch("park")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertFalse(self.client.directory.exists())
        self.factory.assert_not_called()

    def test_ignored_local_key_is_usable_without_disclosing_it(self):
        self.client.directory.mkdir(parents=True)
        key_path = self.client.directory / "api-key"
        key_path.write_text(SECRET + "\n", encoding="utf-8")
        with patch.dict(os.environ, {"DGIS_API_KEY": ""}):
            result = self.client.fetch("park")
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        self.assertEqual(key_path.read_text(encoding="utf-8"), SECRET + "\n")

    def test_status_never_calls_provider_or_reserves_a_new_attempt(self):
        first = self.client.status()
        original = (self.client.directory / "usage.json").read_bytes()
        second = live._LiveClient(self.root, lambda: self.now).status()
        self.assertEqual(first, second)
        self.assertEqual(first["quota"]["used"], 0)
        self.assertEqual(original, (self.client.directory / "usage.json").read_bytes())
        self.factory.assert_not_called()

    def test_reservation_is_durable_before_network_and_errors_count(self):
        def unavailable(*args, **kwargs):
            self.assertEqual(len(self.state()["attempts"]), 1)
            raise URLError("https://bad.example/?key=" + SECRET)
        self.factory.return_value.open.side_effect = unavailable
        with self.assertRaises(live.DGISLiveError) as caught:
            self.client.fetch("park")
        self.assertEqual(caught.exception.quota["used"], 1)
        self.assertEqual(len(self.state()["attempts"]), 1)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("bad.example", str(caught.exception))

    def test_quota_is_shared_and_exhaustion_prevents_network(self):
        self.client.status()
        with self.client._locked():
            self.client._write_attempts([self.now] * (live.LIMIT - 1))
        live._LiveClient(self.root, lambda: self.now).fetch("park")
        self.assertEqual(self.factory.return_value.open.call_count, 1)
        with self.assertRaises(live.DGISLiveError) as caught:
            self.client.fetch("cafe")
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.quota["used"], live.LIMIT)
        self.assertEqual(caught.exception.quota["retry_after_seconds"], live.WINDOW_SECONDS)
        self.assertEqual(self.factory.return_value.open.call_count, 1)
        self.assertFalse(self.client.status()["available"])

    def test_rolling_window_and_restart_do_not_reseed_expired_attempts(self):
        self.client.fetch("park")
        self.now += live.WINDOW_SECONDS + 1
        restarted = live._LiveClient(self.root, lambda: self.now)
        self.assertEqual(restarted.status()["quota"]["used"], 0)
        self.assertEqual(restarted.fetch("park")["quota"]["used"], 1)
        self.assertEqual(len(self.state()["attempts"]), 1)

    def test_existing_ten_timestamps_keep_schema_and_remain_charged_beyond_one_day(self):
        self.assertEqual(live.LIMIT, 500)
        self.assertEqual(live.WINDOW_SECONDS, 30 * 86400)
        # Synthetic prior state, not an adjustment of any production ledger.
        self.client.status()
        timestamps = [self.now - 2 * 86400] + [self.now - index * 60 for index in range(9)]
        with self.client._locked():
            self.client._write_attempts(timestamps)
        path = self.client.directory / "usage.json"
        original = path.read_bytes()
        restarted = live._LiveClient(self.root, lambda: self.now)
        status = restarted.status()
        self.assertEqual(status["quota"]["used"], 10)
        self.assertEqual(status["quota"]["remaining"], 490)
        self.assertEqual(status["quota"]["window_seconds"], 30 * 86400)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.state(), {"schema_version": 1, "attempts": timestamps})
        self.factory.assert_not_called()
        self.now += live.WINDOW_SECONDS + 1
        self.assertEqual(restarted.status()["quota"]["used"], 0)
        self.assertEqual(path.read_bytes(), original)  # Status never resets history.
        self.assertEqual(restarted.fetch("park")["quota"]["used"], 1)
        self.assertEqual(self.state(), {"schema_version": 1, "attempts": [self.now]})

    def test_full_future_window_cannot_escape_cap_through_clock_rollback(self):
        self.client.status()
        with self.client._locked():
            self.client._write_attempts([self.now + live.WINDOW_SECONDS] * live.LIMIT)
        self.now -= live.WINDOW_SECONDS + 1
        restarted = live._LiveClient(self.root, lambda: self.now)
        self.assertEqual(restarted.status()["quota"]["used"], 500)
        self.assertEqual(restarted.status()["quota"]["remaining"], 0)
        with self.assertRaises(live.DGISLiveError) as caught:
            restarted.fetch("park")
        self.assertEqual(caught.exception.http_status, 429)
        self.assertIn("500", str(caught.exception))
        self.assertIn("30 дней", str(caught.exception))
        self.factory.assert_not_called()
        self.assertEqual(len(self.state()["attempts"]), 500)

    def test_explicit_seed_of_prior_attempts_is_local_state_not_portable_magic(self):
        with self.client._locked():
            self.client._write_attempts([self.now, self.now])
        self.assertEqual(self.client.status()["quota"]["used"], 2)
        self.assertEqual(self.client.fetch("park")["quota"]["used"], 3)
        self.assertEqual(set(self.state()), {"schema_version", "attempts"})

    def test_future_timestamps_remain_charged_when_clock_moves_back(self):
        self.client.fetch("park")
        self.now -= 3600
        self.assertEqual(self.client.status()["quota"]["used"], 1)

    def test_corrupt_state_fails_closed_without_echoing_file_contents(self):
        self.client.directory.mkdir(parents=True)
        path = self.client.directory / "usage.json"
        for raw in (SECRET, '[]', '{"schema_version":1,"attempts":[true]}',
                    '{"schema_version":1,"attempts":["anything"]}',
                    '{"schema_version":2,"attempts":[]}'):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(live.DGISLiveError) as caught:
                self.client.fetch("park")
            self.assertEqual(caught.exception.http_status, 503)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertFalse(self.client.status()["available"])
        self.factory.assert_not_called()

    def test_atomic_write_failure_prevents_network_and_removes_temporary_file(self):
        with patch("app.dgis_live.os.replace", side_effect=OSError(SECRET)):
            with self.assertRaises(live.DGISLiveError) as caught:
                self.client.fetch("park")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertFalse(list(self.client.directory.glob(".usage-*")))
        self.factory.assert_not_called()

    def test_http_json_meta_and_large_responses_are_safe_and_charged(self):
        cases = [(HTTPError("https://bad/?key="+SECRET, 403, SECRET, {}, None), None),
                 (None, b"not JSON "+SECRET.encode()),
                 (None, json.dumps({"meta": {"code": 403, "error": SECRET}}).encode()),
                 (None, b"x" * (live.MAX_BYTES + 1)), (TimeoutError(SECRET), None),
                 (URLError(TimeoutError(SECRET)), None)]
        for error, content in cases:
            self.factory.return_value.open.side_effect = error if error else lambda *a, data=content, **k: io.BytesIO(data)
            old = self.client.status()["quota"]["used"]
            with self.assertRaises(live.DGISLiveError) as caught:
                self.client.fetch("park")
            self.assertEqual(caught.exception.quota["used"], old+1)
            if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                self.assertEqual(caught.exception.http_status, 504)
            self.assertNotIn(SECRET, json.dumps(caught.exception.as_dict()))
            self.assertNotIn("https://bad", str(caught.exception))

    def test_redirect_never_follows_or_exposes_target(self):
        with self.assertRaises(live.DGISLiveError) as caught:
            live._NoRedirect().redirect_request(None, None, 302, SECRET, {}, "https://bad/?key="+SECRET)
        self.assertEqual(caught.exception.code, "provider_redirect")
        self.assertNotIn(SECRET, str(caught.exception))
        self.factory.return_value.open.side_effect = caught.exception
        with self.assertRaises(live.DGISLiveError) as fetched:
            self.client.fetch("park")
        self.assertEqual(fetched.exception.quota["used"], 1)

    def test_normalization_filters_out_of_city_deleted_and_limits_one_page(self):
        items = [card(id=str(70000000000000+i)) for i in range(15)]
        items[0] = card(id="1", point={"lat": 60, "lon": 71.4})
        items[1] = card(id="2", dates={"deleted_at": "2026-01-01T00:00:00Z"})
        self.factory.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(envelope(items)).encode())
        result = self.client.fetch("park")
        self.assertEqual(len(result["places"]), 8)
        self.assertEqual(self.factory.return_value.open.call_count, 1)

    def test_demo_compatible_live_count_10_never_requests_another_page(self):
        items = [card(id=str(70000000000000+i)) for i in range(10)]
        payload = envelope(items)
        payload["result"]["total"] = 766
        self.factory.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(payload).encode())
        result = self.client.fetch("park")
        request = self.factory.return_value.open.call_args.args[0]
        params = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(params["page_size"], ["10"])
        self.assertEqual(params["page"], ["1"])
        self.assertEqual(len(result["places"]), 10)
        self.assertEqual(self.factory.return_value.open.call_count, 1)
        self.assertIn("до 10 объектов", result["warnings"][0])

    def test_thread_singleflight_rejects_second_request_without_charge(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return io.BytesIO(json.dumps(envelope()).encode())
        self.factory.return_value.open.side_effect = slow
        def worker():
            try:results.append(self.client.fetch("park"))
            except Exception as error:results.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(live.DGISLiveError) as caught:
                live._LiveClient(self.root, lambda: self.now).fetch("park")
            self.assertEqual(caught.exception.http_status, 409)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], dict)
        self.assertEqual(len(self.state()["attempts"]), 1)

    def test_os_lock_excludes_a_separate_process(self):
        code = """import sys
from app.dgis_live import _LiveClient, DGISLiveError
try:
    with _LiveClient(sys.argv[1])._locked():
        print('unexpected')
except DGISLiveError as error:
    print(error.code)
"""
        with self.client._locked():
            child = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "busy")
        self.factory.assert_not_called()

    def test_public_seams_and_correct_collector_deletion_field(self):
        with patch("app.dgis_live._LiveClient", return_value=self.client):
            self.assertEqual(live.service_status()["quota"]["used"], 0)
            self.assertEqual(live.fetch_live("park")["category"], "park")
        self.assertIn("items.dates.deleted_at", COLLECTOR_FIELDS.split(","))
        self.assertNotIn("items.is_deleted", COLLECTOR_FIELDS.split(","))
        self.assertEqual(normalize_items([card(dates={"deleted_at": "2026-01-01"})], "stamp"), [])


if __name__ == "__main__":
    unittest.main()
