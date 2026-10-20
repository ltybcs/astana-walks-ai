from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.generation import recommend
from app.retrieval import Retriever
from scripts.collect_2gis import CollectionError, NoRedirect, fetch_page, main, normalize_items, opening_hours, response_items


STAMP = "2026-10-20T00:00:00+00:00"
ORIGIN = {"lat": 51.0892, "lon": 71.4074}


def item(**changes):
    value = {"id": "70000000000001", "name": "Тестовое кафе", "type": "branch",
             "point": ORIGIN.copy(), "address_name": "Тестовый адрес, 1",
             "rubrics": [{"name": "Кофейни"}],
             "attribute_groups": [{"attributes": [{"name": "Бесплатный Wi-Fi"}, {"name": "Можно с ноутбуком"}]}]}
    value.update(changes)
    return value


def envelope(items):
    return {"meta": {"code": 200}, "result": {"items": items, "total": len(items)}}


class NormalizationTests(unittest.TestCase):
    def test_wifi_and_laptop_do_not_invent_sockets_or_quietness(self):
        place = normalize_items([item()], STAMP)[0]
        self.assertEqual(place["features"], {"wifi": "yes", "socket": "unknown", "quiet": "unknown", "wheelchair": "unknown"})
        retriever = Retriever(data={"dataset": {"is_demo": False}, "places": [place]})
        response = recommend(retriever, "кофе с розеткой", 60, ORIGIN, use_llm=False)
        self.assertEqual(response["recommendations"], [])

    def test_explicit_source_labels_have_evidence_and_usable_sources(self):
        source = item(attribute_groups=[{"attributes": [{"name": "Есть розетки"}, {"name": "Доступный вход для людей с инвалидностью"}]}])
        place = normalize_items([source], STAMP)[0]
        self.assertEqual(place["features"]["socket"], "yes")
        self.assertEqual(place["features"]["wheelchair"], "yes")
        self.assertEqual(place["feature_evidence"]["socket"], "Есть розетки")
        self.assertEqual(place["source_url"], "https://2gis.kz/astana/firm/70000000000001")
        retriever = Retriever(data={"dataset": {"is_demo": False}, "places": [place]})
        result = recommend(retriever, "кофе с розеткой", 60, ORIGIN, use_llm=False)
        self.assertEqual(result["recommendations"][0]["id"], place["id"])
        self.assertEqual(result["sources"][0]["url"], place["source_url"])
        self.assertIn("не подтверждены", result["recommendations"][0]["opening_note"])

    def test_filters_bad_geometry_wrong_city_closed_deleted_and_unrelated_results(self):
        malformed = [None, [], item(point=None), item(point={"lat": True, "lon": 71.4}),
                     item(point={"lat": float("nan"), "lon": 71.4}), item(point={"lat": 55.0, "lon": 71.4}),
                     item(flags={"temporary_closed": True}), item(is_deleted=True),
                     item(rubrics=[{"name": "Строительные магазины"}]), item(id="../../bad")]
        self.assertEqual(normalize_items(malformed, STAMP), [])

    def test_deduplicates_and_removes_opaque_id_suffix(self):
        places = normalize_items([item(), item(id="70000000000001_opaque"), item()], STAMP)
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["id"], "2gis:70000000000001")

    def test_geo_attraction_has_geo_source_and_does_not_use_review_text(self):
        source = item(type="attraction", rubrics=[], reviews={"text": "Есть розетки и очень тихо"})
        place = normalize_items([source], STAMP)[0]
        self.assertEqual(place["category"], "attraction")
        self.assertIn("/geo/", place["source_url"])
        self.assertEqual(place["features"]["socket"], "unknown")
        self.assertNotIn("reviews", place)

    def test_only_complete_explicit_week_without_special_schedule_can_mean_24_7(self):
        schedule = {day: {"working_hours": [{"from": "00:00", "to": "24:00"}]} for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
        self.assertEqual(opening_hours(item(schedule=schedule)), "24/7")
        incomplete = deepcopy(schedule)
        del incomplete["Sun"]
        self.assertIsNone(opening_hours(item(schedule=incomplete)))
        self.assertNotEqual(opening_hours(item(schedule=schedule, schedule_special={"note": "holiday"})), "24/7")
        ambiguous = deepcopy(schedule)
        ambiguous["Sun"]["working_hours"][0]["to"] = "00:00"
        self.assertNotEqual(opening_hours(item(schedule=ambiguous)), "24/7")

    def test_official_item_not_found_is_empty_only_without_result_conflicts(self):
        empty = {"meta": {"code": 404, "error": {"type": "itemNotFound"}}}
        original = deepcopy(empty)
        self.assertEqual(response_items(empty), [])
        self.assertEqual(empty, original)
        invalid = [{"meta": {"code": code, "error": {"type": "itemNotFound"}}}
                   for code in (401, 403, 500, "404", 404.0, True)]
        invalid += [{"meta": {"code": 404}}, {"meta": {"code": 404, "error": []}},
                    {"meta": {"code": 404, "error": {"type": "otherNotFound"}}},
                    {**empty, "result": {"items": [item()]}},
                    {**empty, "result": {}}, {**empty, "items": [item()]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CollectionError):
                response_items(value)

    def test_rejects_failure_and_incomplete_api_envelopes(self):
        for payload in (None, [], {}, {"meta": {"code": 403}}, {"meta": {"code": 200}, "result": {}}):
            with self.assertRaises(CollectionError):
                response_items(payload)


class CollectionTests(unittest.TestCase):
    def test_api_request_uses_documented_bbox_and_field_names(self):
        with patch("scripts.collect_2gis.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps(envelope([item()])).encode())
            result = fetch_page("dummy-key", "Кофейни", (51.02, 71.30, 51.22, 71.55), 1)
            url = factory.return_value.open.call_args.args[0].full_url
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "catalog.api.2gis.com")
        self.assertEqual(params["point1"], ["71.3,51.22"])
        self.assertEqual(params["point2"], ["71.55,51.02"])
        self.assertEqual(params["locale"], ["ru_KZ"])
        self.assertEqual(params["page_size"], ["10"])
        self.assertIn("items.point", params["fields"][0])
        self.assertEqual(response_items(result)[0]["name"], "Тестовое кафе")

    def test_network_errors_never_echo_keys_or_body(self):
        secret = "dummy-sensitive-key"
        errors = [URLError("https://example.com/?key=" + secret), HTTPError("https://example.com/?key=" + secret, 403, secret, {}, None)]
        for error in errors:
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            with patch("scripts.collect_2gis.build_opener") as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(CollectionError) as caught:
                    fetch_page(secret, "Кофейни", (51.02, 71.30, 51.22, 71.55), 1)
                self.assertNotIn(secret, str(caught.exception))

    def test_redirect_is_not_followed(self):
        with self.assertRaises(CollectionError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_replay_is_loadable_and_marked_source_data_not_demo(self):
        with tempfile.TemporaryDirectory() as root:
            source, output = Path(root) / "api.json", Path(root) / "poi.json"
            source.write_text(json.dumps(envelope([item()])), encoding="utf-8")
            self.assertEqual(main(["--input", str(source), "--output", str(output)]), 0)
            retriever = Retriever(output)
            self.assertFalse(retriever.dataset["is_demo"])
            self.assertEqual(retriever.dataset["collection_method"], "2gis_api_replay")
            self.assertEqual(retriever.dataset["license"], "2GIS API terms (not ODbL)")
            self.assertEqual(len(retriever.places), 1)

    def test_missing_key_empty_or_corrupt_inputs_preserve_existing_dataset(self):
        with tempfile.TemporaryDirectory() as root:
            output, source = Path(root) / "existing.json", Path(root) / "api.json"
            output.write_text("existing data", encoding="utf-8")
            with patch.dict("os.environ", {"DGIS_API_KEY": ""}), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main(["--output", str(output)]), 1)
            for content in ("not JSON", json.dumps(envelope([])), json.dumps({"meta": {"code": 403}})):
                source.write_text(content, encoding="utf-8")
                with patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(main(["--input", str(source), "--output", str(output)]), 1)
                self.assertEqual(output.read_text(encoding="utf-8"), "existing data")

    def test_network_pagination_is_bounded_and_stops_on_short_page(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "poi.json"
            with patch.dict("os.environ", {"DGIS_API_KEY": "dummy-key"}), patch("scripts.collect_2gis.fetch_page", return_value=envelope([item()])) as fetch:
                self.assertEqual(main(["--query", "Кофейни", "--pages", "3", "--output", str(output)]), 0)
                self.assertEqual(fetch.call_count, 1)
                self.assertNotIn("dummy-key", output.read_text(encoding="utf-8"))

    def test_default_demo_sized_pagination_uses_ten_and_stops_at_total(self):
        # Synthetic payloads exercise page arithmetic, not live key permissions.
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "poi.json"
            def page(key, query, bbox, number, size):
                self.assertEqual(size, 10)
                start = (number-1)*size
                rows = [item(id=str(70000000000000+i)) for i in range(start, min(start+size, 25))]
                payload = envelope(rows)
                payload["result"]["total"] = 25
                return payload
            with patch.dict("os.environ", {"DGIS_API_KEY": "dummy-key"}), patch("scripts.collect_2gis.fetch_page", side_effect=page) as fetch, patch("scripts.collect_2gis.time.sleep"):
                self.assertEqual(main(["--query", "Кофейни", "--pages", "5", "--output", str(output)]), 0)
            self.assertEqual([call.args[3] for call in fetch.call_args_list], [1, 2, 3])
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(document["places"]), 25)
            self.assertEqual(document["dataset"]["page_size"], 10)

    def test_explicit_subscription_page_size_50_uses_its_own_pagination(self):
        # The paid size is deliberately explicit and still only a mocked fixture.
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "poi.json"
            def page(key, query, bbox, number, size):
                self.assertEqual(size, 50)
                start = (number-1)*size
                rows = [item(id=str(70000000000000+i)) for i in range(start, min(start+size, 60))]
                payload = envelope(rows)
                payload["result"]["total"] = 60
                return payload
            with patch.dict("os.environ", {"DGIS_API_KEY": "dummy-key"}), patch("scripts.collect_2gis.fetch_page", side_effect=page) as fetch, patch("scripts.collect_2gis.time.sleep"):
                self.assertEqual(main(["--query", "Кофейни", "--pages", "3", "--page-size", "50", "--output", str(output)]), 0)
            self.assertEqual([call.args[3] for call in fetch.call_args_list], [1, 2])
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(document["places"]), 60)
            self.assertEqual(document["dataset"]["page_size"], 50)

    def test_page_size_outside_documented_range_never_reaches_network(self):
        for value in (0, 51, True, "10"):
            with self.subTest(value=value), patch("scripts.collect_2gis.build_opener") as opener:
                with self.assertRaises(CollectionError):
                    fetch_page("dummy-key", "Кофейни", (51.02, 71.30, 51.22, 71.55), 1, value)
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
