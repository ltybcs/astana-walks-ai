"""Offline unified 2GIS contract: only synthetic cards and a temporary ledger."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler

from app import dgis_live as legacy
from app import unified_dgis as module
from app.category_catalog import CATEGORY_CATALOG
from tests.test_dgis_live import SECRET, card, envelope

ORIGIN = {"lat": 51.1546, "lon": 71.4252, "label": "PRIVATE_LABEL"}


class Response(io.BytesIO):
    def __init__(self, raw=None, *, mime="application/json"):
        super().__init__(json.dumps(envelope()).encode() if raw is None else raw)
        self.headers = {"Content-Type": mime}


class UnifiedDGISTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1790000000.0
        self.legacy = legacy._LiveClient(self.root, lambda: self.now)
        environment = patch.dict(os.environ, {"DGIS_API_KEY": SECRET})
        environment.start()
        self.addCleanup(environment.stop)
        # Only the old primary service may initialize its own existing budget.
        self.legacy.status()
        self.transport = Mock(side_effect=lambda *args, **kwargs: Response())
        self.client = module.UnifiedDGIS(self.root, http_open=self.transport, clock=lambda: self.now)

    def state(self):
        return json.loads((self.legacy.directory / "usage.json").read_bytes())

    def test_nearby_query_is_fixed_category_and_official_box_with_hard_circle(self):
        # Corner lies inside the bbox but outside the actual circle.
        items = [card(), card(id="2", point={"lat": ORIGIN["lat"] + .008,
                                             "lon": ORIGIN["lon"] + .012}),
                 card(id="3", point={"lat": 52, "lon": 71.4})]
        self.transport.side_effect = lambda *args, **kwargs: Response(json.dumps(envelope(items)).encode())
        query = "Хочу прогулку в парке PRIVATE_RAW_QUERY"
        before = deepcopy(ORIGIN)
        result = self.client.search(query, ORIGIN, 1000)
        request = self.transport.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path),
                         ("https", "catalog.api.2gis.com", "/3.0/items"))
        params = parse_qs(parsed.query)
        self.assertEqual(params["q"], [CATEGORY_CATALOG["park"]["dgis_query"]])
        self.assertEqual(params["page"], ["1"])
        self.assertEqual(params["page_size"], ["10"])
        self.assertEqual(params["fields"], ["items.point,items.rubrics"])
        self.assertEqual(set(params), {"q", "key", "point1", "point2", "page", "page_size", "fields"})
        west, north = map(float, params["point1"][0].split(","))
        east, south = map(float, params["point2"][0].split(","))
        self.assertLess(west, ORIGIN["lon"])
        self.assertLess(ORIGIN["lon"], east)
        self.assertLess(south, ORIGIN["lat"])
        self.assertLess(ORIGIN["lat"], north)
        self.assertNotIn("PRIVATE", request.full_url)
        self.assertEqual(ORIGIN, before)
        self.assertEqual([p["id"] for p in result["places"]], ["2gis:70000000000001"])
        self.assertEqual(result["places"][0]["features"]["quiet"], "unknown")
        self.assertEqual(result["search_scope"], {"radius_meters": 1000, "returned_count": 3, "filtered_count": 2})
        self.assertEqual(result["attribution"], legacy.ATTRIBUTION)
        self.assertEqual(self.transport.call_count, 1)

    def test_empty_results_and_ten_card_page_do_not_trigger_another_request(self):
        for items in ([], [card(id=str(70000000000000+i), rubrics=[{"name": "Кофейни"}]) for i in range(15)]):
            self.transport.side_effect = lambda *a, values=items, **k: Response(json.dumps(envelope(values)).encode())
            result = self.client.search("кофе", ORIGIN, 1000)
            self.assertEqual(len(result["places"]), min(10, len(items)))
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(result["category"], "cafe")

    def test_constructor_requires_explicit_absolute_root_and_bounded_timeout(self):
        for root, timeout in (("relative", 25), (self.root, 0), (self.root, 26), (self.root, True), (self.root, float("nan"))):
            with self.subTest(root=root, timeout=timeout), self.assertRaises(ValueError):
                module.UnifiedDGIS(root, timeout=timeout)
        self.transport.assert_not_called()

    def test_invalid_search_is_rejected_before_key_storage_or_network(self):
        with patch.object(self.client._client, "_key", side_effect=AssertionError("Must validate first")):
            for query, origin, radius in (("", ORIGIN, 1000), (None, ORIGIN, 1000),
                                          ("park", {"lat": True, "lon": 71.4}, 1000),
                                          ("park", {"lat": float("nan"), "lon": 71.4}, 1000),
                                          ("park", {"lat": 52, "lon": 71.4}, 1000),
                                          ("park", ORIGIN, 99), ("park", ORIGIN, 2001),
                                          ("park", ORIGIN, True)):
                with self.subTest(query=query, origin=origin, radius=radius), self.assertRaises(legacy.DGISLiveError) as caught:
                    self.client.search(query, origin, radius)
                self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(self.state()["attempts"], [])
        self.transport.assert_not_called()

    def test_missing_ledger_never_seeds_a_second_worktree_budget(self):
        (self.legacy.directory / "usage.json").unlink()
        with patch.object(self.client._client, "_key", side_effect=AssertionError("No key read")):
            status = self.client.status()
            with self.assertRaises(legacy.DGISLiveError) as caught:
                self.client.search("парк", ORIGIN, 1000)
        self.assertEqual(status["code"], "quota_state_unavailable")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertFalse((self.legacy.directory / "usage.json").exists())
        self.transport.assert_not_called()

    def test_corrupt_or_nonfinite_ledger_fails_closed_and_is_not_overwritten(self):
        path = self.legacy.directory / "usage.json"
        for raw in ('PRIVATE_STATE', '[]', '{"schema_version":1,"attempts":[NaN]}',
                    '{"schema_version":1,"attempts":[1e999]}', '{"schema_version":1,"attempts":[true]}',
                    '{"schema_version":1,"schema_version":1,"attempts":[]}',
                    '{"schema_version":2,"attempts":[]}'):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(legacy.DGISLiveError) as caught:
                self.client.search("парк", ORIGIN, 1000)
            self.assertEqual(caught.exception.code, "quota_state_unavailable")
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
        self.transport.assert_not_called()

    def test_status_is_read_only_and_window_handles_restart_and_clock_rollback(self):
        before = (self.legacy.directory / "usage.json").read_bytes()
        self.assertEqual(self.client.status()["quota"]["used"], 0)
        self.assertEqual(before, (self.legacy.directory / "usage.json").read_bytes())
        self.transport.assert_not_called()
        self.client.search("парк", ORIGIN, 1000)
        self.now -= 3600
        restarted = module.UnifiedDGIS(self.root, http_open=self.transport, clock=lambda: self.now)
        self.assertEqual(restarted.status()["quota"]["used"], 1)
        self.now += 90001
        self.assertEqual(restarted.status()["quota"]["used"], 1)  # Older than 24h still charged.
        self.now += legacy.WINDOW_SECONDS
        self.assertEqual(restarted.status()["quota"]["used"], 0)

    def test_legacy_and_unified_share_five_hundred_attempts_and_same_lock(self):
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(json.dumps(envelope()).encode())
        # Keep transport work bounded: existing synthetic timestamps plus the
        # same twenty new alternating requests reach the shared 500-attempt cap.
        with self.legacy._locked():
            self.legacy._write_attempts([self.now] * (legacy.LIMIT - 20))
        with patch("app.dgis_live.build_opener", return_value=opener):
            for _ in range(10):
                self.legacy.fetch("park")
                self.client.search("парк", ORIGIN, 1000)
        self.assertEqual(len(self.state()["attempts"]), legacy.LIMIT)
        self.assertEqual(self.legacy.status()["quota"]["used"], legacy.LIMIT)
        self.assertEqual(self.client.status()["quota"]["used"], legacy.LIMIT)
        for action in (lambda: self.client.search("парк", ORIGIN, 1000), lambda: self.legacy.fetch("park")):
            with self.assertRaises(legacy.DGISLiveError) as caught:
                action()
            self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(self.transport.call_count, 10)
        self.assertEqual(opener.open.call_count, 10)
        restarted = module.UnifiedDGIS(self.root, http_open=self.transport, clock=lambda: self.now)
        self.assertEqual(restarted.status()["quota"]["limit"], 500)
        self.assertEqual(restarted.status()["quota"]["used"], 500)
        with self.assertRaises(legacy.DGISLiveError) as caught:
            restarted.search("парк", ORIGIN, 1000)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertIn("30 дней", str(caught.exception))
        self.assertEqual(self.transport.call_count, 10)
        with self.legacy._locked():
            with self.assertRaises(legacy.DGISLiveError) as caught:
                self.client.search("парк", ORIGIN, 1000)
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(len(self.state()["attempts"]), legacy.LIMIT)

    def test_reservation_precedes_transport_and_no_content_is_persisted(self):
        def unavailable(request, timeout):
            self.assertEqual(len(self.state()["attempts"]), 1)
            raise URLError("PRIVATE_RAW_QUERY https://bad.example/?key=" + SECRET)
        self.transport.side_effect = unavailable
        with self.assertRaises(legacy.DGISLiveError) as caught:
            self.client.search("парк PRIVATE_RAW_QUERY", ORIGIN, 1000)
        self.assertEqual(caught.exception.quota["used"], 1)
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual({p.name for p in self.legacy.directory.iterdir()}, {"usage.json", "usage.lock"})
        for path in self.legacy.directory.iterdir():
            raw = path.read_bytes()
            for marker in (SECRET.encode(), b"PRIVATE", b"places", "Синтетический парк".encode()):
                self.assertNotIn(marker, raw)

    def test_atomic_reservation_failure_prevents_http_and_leaves_no_temporary_file(self):
        with patch("app.dgis_live.os.replace", side_effect=OSError("PRIVATE_STORAGE")):
            with self.assertRaises(legacy.DGISLiveError) as caught:
                self.client.search("парк", ORIGIN, 1000)
        self.assertEqual(caught.exception.code, "quota_state_unavailable")
        self.assertFalse(list(self.legacy.directory.glob(".usage-*")))
        self.transport.assert_not_called()

    def test_total_read_deadline_catches_slow_trickle_and_charges_only_once(self):
        elapsed = [0.0]
        class Slow(Response):
            def read1(self, length):
                elapsed[0] += .6
                return b" "
        self.transport.side_effect = lambda *a, **k: Slow()
        client = module.UnifiedDGIS(self.root, http_open=self.transport, clock=lambda: self.now,
                                    monotonic_clock=lambda: elapsed[0], timeout=1)
        with self.assertRaises(legacy.DGISLiveError) as caught:
            client.search("парк", ORIGIN, 1000)
        self.assertEqual(caught.exception.code, "provider_timeout")
        self.assertEqual(caught.exception.quota["used"], 1)
        self.assertEqual(elapsed[0], 1.2)
        self.assertEqual(self.transport.call_count, 1)

    def test_invalid_mime_json_duplicate_nonfinite_and_oversize_are_sanitized(self):
        raws = [(b"{}", "text/html"), (b"\xff", "application/json"),
                (b'{"meta":{"code":200},"result":{"items":[]},"x":1e999}', "application/json"),
                (b'{"meta":{"code":200},"result":{"items":[]},"x":NaN}', "application/json"),
                (b'{"meta":{},"meta":{}}', "application/json"),
                (b"PRIVATE " * 100, "application/json")]
        with patch.object(module, "MAX_BYTES", 500):
            for raw, mime in raws:
                self.transport.side_effect = lambda *a, data=raw, content=mime, **k: Response(data, mime=content)
                old = len(self.state()["attempts"])
                with self.subTest(raw=raw), self.assertRaises(legacy.DGISLiveError) as caught:
                    self.client.search("парк", ORIGIN, 1000)
                self.assertEqual(caught.exception.quota["used"], old + 1)
                self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.transport.call_count, len(raws))

    def test_production_transport_disables_proxy_redirect_and_counts_errors(self):
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response()
        client = module.UnifiedDGIS(self.root, clock=lambda: self.now)
        with patch("app.unified_dgis.build_opener", return_value=opener) as factory:
            client.search("парк", ORIGIN, 1000)
        self.assertTrue(any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in factory.call_args.args))
        redirect = next(handler for handler in factory.call_args.args if isinstance(handler, legacy._NoRedirect))
        with self.assertRaises(legacy.DGISLiveError) as caught:
            redirect.redirect_request(None, None, 302, "PRIVATE", {}, "https://evil.invalid/" + SECRET)
        self.assertEqual(caught.exception.code, "provider_redirect")
        self.transport.side_effect = caught.exception
        with self.assertRaises(legacy.DGISLiveError) as fetched:
            self.client.search("парк", ORIGIN, 1000)
        self.assertEqual(fetched.exception.quota["used"], 2)
        self.assertNotIn(SECRET, str(fetched.exception))


    def test_computer_club_aliases_send_only_allowlisted_query_with_explicit_rubric(self):
        item = card(type="branch", name="Синтетическое место для проверки категории",
                    rubrics=[{"name": "Компьютерные клубы"}])
        self.transport.side_effect = lambda *a, **k: Response(json.dumps(envelope([item])).encode())
        for alias in ("комп клуб", "компьютерный клуб", "киберклуб", "интернет-клуб",
                      "computer club", "gaming lounge", "PC club"):
            with self.subTest(alias=alias):
                result = self.client.search("Хочу в " + alias + " PRIVATE_RAW_QUERY", ORIGIN, 1000)
                params = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
                self.assertEqual(params["q"], ["Компьютерные клубы"])
                self.assertNotIn("PRIVATE", self.transport.call_args.args[0].full_url)
                self.assertEqual(result["category"], "computer_club")
                self.assertEqual([place["category"] for place in result["places"]], ["computer_club"])
                self.assertEqual(result["places"][0]["source_rubrics"], ["Компьютерные клубы"])
                self.assertEqual(result["places"][0]["features"]["socket"], "unknown")
                self.assertIsNone(result["places"][0]["opening_hours"])
        self.assertEqual(self.transport.call_count, 7)
        self.assertNotIn("computer_club", legacy.CATEGORIES)

    def test_computer_club_is_supported_only_by_exact_rubrics_not_brand_or_query(self):
        # Source order must not hide an explicit club rubric behind a cafe.
        items = [card(id="1", type="branch", name="Нейтральное имя", rubrics=[{"name": "Компьютерные клубы"}]),
                 card(id="2", type="branch", name="Другое имя", rubrics=[{"name": "Интернет-клубы"}]),
                 card(id="3", type="branch", name="Третье имя", rubrics=[{"name": "Кафе"}, {"name": "Компьютерные клубы"}]),
                 card(id="4", type="branch", name="Gaming Club — только бренд", rubrics=[{"name": "Кафе"}]),
                 card(id="5", type="branch", name="Компьютерный клуб — только имя", rubrics=[]),
                 card(id="6", type="branch", name="Клуб", rubrics=[{"name": "Компьютерный ремонт"}])]
        self.transport.side_effect = lambda *a, **k: Response(json.dumps(envelope(items)).encode())
        result = self.client.search("computer club", ORIGIN, 1000)
        self.assertEqual([place["id"] for place in result["places"]], ["2gis:1", "2gis:2", "2gis:3"])
        self.assertTrue(all(place["category"] == "computer_club" for place in result["places"]))
        self.assertEqual(result["search_scope"]["filtered_count"], 3)
        self.assertEqual(self.transport.call_count, 1)

    def test_unrelated_cafe_or_park_results_do_not_replace_requested_computer_club(self):
        items = [card(type="branch", name="Gaming Club Cafe", rubrics=[{"name": "Кафе"}]), card(id="2")]
        self.transport.side_effect = lambda *a, **k: Response(json.dumps(envelope(items)).encode())
        result = self.client.search("Хочу в комп клуб", ORIGIN, 1000)
        self.assertEqual(result["places"], [])
        self.assertEqual(result["category"], "computer_club")
        self.assertEqual(result["search_scope"]["filtered_count"], 2)
        self.assertEqual(self.transport.call_count, 1)

    def test_catalog_types_require_their_own_explicit_rubric_and_one_fixed_query(self):
        for identifier, spec in CATEGORY_CATALOG.items():
            if not spec["rubric_aliases"]:
                continue
            with self.subTest(identifier=identifier):
                source = card(type="branch", name="Нейтральное синтетическое название",
                              rubrics=[{"name": spec["rubric_aliases"][0]}])
                self.transport.side_effect = lambda *a, item=source, **k: Response(json.dumps(envelope([item])).encode())
                before = self.transport.call_count
                result = self.client.search("Хочу " + spec["ru_label"] + " возле Mega PRIVATE_MARKER", ORIGIN, 1000)
                params = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
                self.assertEqual(params["q"], [spec["dgis_query"]])
                self.assertNotIn("PRIVATE_MARKER", self.transport.call_args.args[0].full_url)
                self.assertEqual([p["category"] for p in result["places"]], [identifier])
                self.assertIn(identifier, result["places"][0]["source_categories"])
                self.assertEqual(self.transport.call_count, before + 1)

    def test_multirubric_source_can_prove_requested_type_but_brand_and_secondary_goal_cannot(self):
        items = [card(id="1", type="branch", name="Нейтральное название",
                      rubrics=[{"name": "Кафе"}, {"name": "Боулинг"}]),
                 card(id="2", type="branch", name="Bowling — это только бренд",
                      rubrics=[{"name": "Кафе"}]),
                 card(id="3", type="branch", name="Боулинг без рубрики", rubrics=[])]
        self.transport.side_effect = lambda *a, **k: Response(json.dumps(envelope(items)).encode())
        result = self.client.search("Хочу в боулинг и кафе возле Mega", ORIGIN, 1000)
        params = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
        self.assertEqual(params["q"], [CATEGORY_CATALOG["bowling"]["dgis_query"]])
        self.assertEqual(result["categories"], ["bowling"])
        self.assertEqual([p["id"] for p in result["places"]], ["2gis:1"])
        self.assertEqual(result["places"][0]["category"], "bowling")
        self.assertEqual(result["places"][0]["source_categories"], ["cafe", "bowling"])
        self.assertEqual(result["places"][0]["source_rubrics"], ["Кафе", "Боулинг"])
        self.assertEqual(self.transport.call_count, 1)

    def test_or_categories_share_one_request_and_reject_an_unrequested_park(self):
        items = [card(id="1", type="branch", rubrics=[{"name": "Детские площадки"}]),
                 card(id="2", type="branch", rubrics=[{"name": "Музеи"}]), card(id="3")]
        self.transport.side_effect = lambda *a, **k: Response(json.dumps(envelope(items)).encode())
        result = self.client.search("Хочу детскую площадку или музей возле Mega", ORIGIN, 1000)
        params = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
        self.assertEqual(params["q"], ["; ".join(CATEGORY_CATALOG[c]["dgis_query"] for c in ("playground", "museum"))])
        self.assertEqual(result["categories"], ["playground", "museum"])
        self.assertEqual([p["category"] for p in result["places"]], ["playground", "museum"])
        self.assertEqual(self.transport.call_count, 1)

    def test_unknown_explicit_activity_is_rejected_before_key_ledger_or_http(self):
        with patch.object(self.client._client, "_key", side_effect=AssertionError("No key access")), \
             patch.object(self.client, "_existing_state", side_effect=AssertionError("No ledger access")):
            with self.assertRaises(legacy.DGISLiveError) as caught:
                self.client.search("Хочу в обсерваторию", ORIGIN, 1000)
        self.assertEqual(caught.exception.code, "unsupported_activity")
        self.assertEqual(caught.exception.http_status, 422)
        self.assertEqual(self.state()["attempts"], [])
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
