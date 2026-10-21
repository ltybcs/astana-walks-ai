"""Offline own-field/provenance tests; no source requests or venue assertions."""
from copy import deepcopy
import json
import unittest

from app.place_evidence import PlaceEvidenceError, VERIFIED_DGIS_ATTRIBUTES, normalize_place_evidence

STAMP = "2026-10-21T12:30:00Z"
URLS = {"osm": "https://www.openstreetmap.org/node/123",
        "dgis": "https://2gis.kz/astana/firm/123",
        "google": "https://www.google.com/maps?cid=123"}


def project(place=None, provider="osm", **kwargs):
    return normalize_place_evidence({"id": "123"} if place is None else place,
        provider=provider, place_id=kwargs.get("place_id", "123"),
        source_url=kwargs.get("source_url", URLS.get(provider, "https://example.com/")),
        observed_at=kwargs.get("observed_at", STAMP))


class PlaceEvidenceTests(unittest.TestCase):
    def test_missing_properties_remain_unknown_without_fabricated_evidence(self):
        for provider in URLS:
            with self.subTest(provider=provider):
                self.assertEqual(project(provider=provider), {
                    "features": {"socket": "unknown", "wifi": "unknown", "quiet": "unknown", "wheelchair": "unknown"},
                    "evidence": []})

    def test_existing_normalized_features_keep_yes_no_unknown_and_honest_path(self):
        for provider in ("osm", "dgis"):
            with self.subTest(provider=provider):
                result = project({"id": "123", "features": {"socket": "yes", "quiet": "no", "wheelchair": "unknown"}}, provider)
                self.assertEqual(result["features"], {"socket": "yes", "wifi": "unknown", "quiet": "no", "wheelchair": "unknown"})
                for item in result["evidence"]:
                    self.assertEqual(set(item), {"property", "value", "provider", "place_id", "field_path", "source_url", "observed_at"})
                    self.assertEqual(item["provider"], provider)
                    self.assertEqual(item["place_id"], "123")
                    self.assertEqual(item["field_path"], "normalized.features." + item["property"])
                    self.assertEqual(item["source_url"], URLS[provider])
                    self.assertEqual(item["observed_at"], STAMP)

    def test_boolean_false_is_negative_not_unknown_or_truthy_string(self):
        self.assertEqual(project({"id": "123", "features": {"socket": True, "quiet": False}})["features"],
                         {"socket": "yes", "wifi": "unknown", "quiet": "no", "wheelchair": "unknown"})
        result = project({"id": "123", "accessibilityOptions": {"wheelchairAccessibleEntrance": False}}, "google")
        self.assertEqual(result["features"]["wheelchair"], "no")
        self.assertEqual(result["evidence"][0]["value"], "no")

    def test_google_entrance_is_only_entrance_not_accessible_route(self):
        result = project({"id": "123", "accessibilityOptions": {"wheelchairAccessibleEntrance": True,
            "wheelchairAccessibleParking": False, "wheelchairAccessibleRestroom": False,
            "wheelchairAccessibleSeating": False}}, "google")
        self.assertEqual(result["features"], {"socket": "unknown", "wifi": "unknown", "quiet": "unknown", "wheelchair": "yes"})
        self.assertEqual(result["evidence"][0]["field_path"], "accessibilityOptions.wheelchairAccessibleEntrance")
        self.assertNotIn("route", json.dumps(result))
        parking_only = project({"id": "123", "accessibilityOptions": {"wheelchairAccessibleParking": True}}, "google")
        self.assertEqual(parking_only["features"]["wheelchair"], "unknown")
        self.assertEqual(parking_only["evidence"], [])

    def test_groups_are_not_capacity_and_no_socket_quiet_fields_are_invented(self):
        result = project({"id": "123", "goodForGroups": True, "goodForChildren": True,
            "servesCoffee": True, "reservable": True, "features": {"socket": "yes", "quiet": "yes"},
            "description": "Quiet cafe with power outlets", "rating": 5, "capacity": 5}, "google")
        self.assertEqual(result, project(provider="google"))
        self.assertNotIn("capacity", json.dumps(result))

    def test_unverified_dgis_ids_tags_and_names_are_not_property_proof(self):
        self.assertEqual(dict(VERIFIED_DGIS_ATTRIBUTES), {})
        result = project({"id": "123", "attribute_groups": [{"name": "Amenities", "rubric_ids": ["cafe"],
            "attributes": [{"id": "unverified-socket", "tag": "socket", "name": "Розетки"},
                           {"id": "unverified-quiet", "tag": "quiet", "name": "Тихое место"},
                           {"id": "unverified-access", "tag": "wheelchair", "name": "Доступно"}]}]}, "dgis")
        self.assertEqual(result, project(provider="dgis"))

    def test_category_and_prose_do_not_establish_specific_service_or_inventory(self):
        result = project({"id": "123", "category": "computer_club", "source_rubrics": ["Компьютерные клубы"],
            "description": "RTX, free seats and tournaments", "services": ["tournament"],
            "equipment": {"RTX": True}, "inventory": "available", "opening_hours": "24/7"}, "dgis")
        self.assertEqual(result, project(provider="dgis"))

    def test_identity_mismatch_never_transfers_another_branch_property(self):
        with self.assertRaises(PlaceEvidenceError):
            project({"id": "other-branch", "features": {"socket": "yes"}})
        with self.assertRaises(PlaceEvidenceError):
            project({"id": "123", "provider": "dgis", "features": {"socket": "yes"}})
        with self.assertRaises(PlaceEvidenceError):
            project({"features": {"socket": "yes"}})
        first = project({"id": "123", "features": {"socket": "yes"}})
        second = project({"id": "124"}, place_id="124", source_url="https://www.openstreetmap.org/node/124")
        self.assertEqual(first["features"]["socket"], "yes")
        self.assertEqual(second["features"]["socket"], "unknown")

    def test_input_and_output_are_detached_and_no_provider_body_is_returned(self):
        original = {"id": "123", "features": {"socket": "yes"}, "lat": 51.1, "lon": 71.4,
                    "name": "Private unused name", "query": "Private query", "reviews": [{"text": "secret review"}]}
        before = deepcopy(original)
        result = project(original)
        self.assertEqual(original, before)
        self.assertEqual(set(result), {"features", "evidence"})
        serialized = json.dumps(result)
        for marker in ("Private", "secret review", "51.1", "71.4", "reviews", "query"):
            self.assertNotIn(marker, serialized)
        result["features"]["socket"] = "no"
        result["evidence"][0]["value"] = "no"
        self.assertEqual(project(original)["features"]["socket"], "yes")

    def test_invalid_top_shapes_and_feature_values_fail_closed(self):
        for record in (None, [], "place", {"id": "123", "features": []},
                       {"id": "123", "features": {"socket": 1}},
                       {"id": "123", "features": {"quiet": "false"}},
                       {"id": "123", "features": {"wheelchair": None}}):
            with self.subTest(record=record):
                with self.assertRaises(PlaceEvidenceError):
                    normalize_place_evidence(record, provider="osm", place_id="123", source_url=URLS["osm"], observed_at=STAMP)
        for provider in ("other", None, [], 1):
            with self.subTest(provider=provider):
                with self.assertRaises(PlaceEvidenceError):
                    normalize_place_evidence({"id": "123"}, provider=provider, place_id="123",
                        source_url=URLS["osm"], observed_at=STAMP)

    def test_google_boolean_types_are_not_coerced(self):
        for options in (None, [], {"wheelchairAccessibleEntrance": 1}, {"wheelchairAccessibleEntrance": "true"},
                        {"wheelchairAccessibleEntrance": "yes"}, {"wheelchairAccessibleEntrance": None}):
            with self.subTest(options=options):
                with self.assertRaises(PlaceEvidenceError):
                    project({"id": "123", "accessibilityOptions": options}, "google")

    def test_attribute_shapes_are_bounded_and_no_raw_values_leak_in_errors(self):
        invalid = (None, {}, [None], [{"attributes": {}}], [{"attributes": [None]}],
                   [{"attributes": [{"id": 123}]}], [{"attributes": [{"id": "x", "tag": 1}]}],
                   [{}] * 17, [{"attributes": [{"id": "x"}] * 65}])
        for groups in invalid:
            with self.subTest(groups=groups):
                with self.assertRaises(PlaceEvidenceError) as caught:
                    project({"id": "123", "attribute_groups": groups}, "dgis")
                self.assertEqual(str(caught.exception), "Некорректные структурированные сведения о месте.")

    def test_source_url_bounds_and_provider_identity_fail_closed(self):
        for url in ("http://www.openstreetmap.org/node/123", "https://evil.example/node/123",
                    "https://www.openstreetmap.org.evil.example/node/123", "https://secret@www.openstreetmap.org/node/123",
                    "https://www.openstreetmap.org:bad/node/123", "https://www.openstreetmap.org/node/123#secret",
                    "https://www.openstreetmap.org/node/123?key=PRIVATE", "https://www.openstreetmap.org/a\nsecret",
                    "https://www.openstreetmap.org/" + "a" * 2048):
            with self.subTest(url=url):
                with self.assertRaises(PlaceEvidenceError) as caught:
                    project(source_url=url)
                self.assertNotIn("PRIVATE", str(caught.exception))
        with self.assertRaises(PlaceEvidenceError):
            project({"id": "bad id"}, place_id="bad id")

    def test_observation_time_requires_timezone_and_normalizes_same_instant_to_utc(self):
        result = project({"id": "123", "features": {"quiet": "unknown"}}, observed_at="2026-10-21T17:30:00+05:00")
        self.assertEqual(result["evidence"][0]["observed_at"], STAMP)
        # No unrequested freshness guarantee: historical observation stays historical.
        result = project({"id": "123", "features": {"socket": "no"}}, observed_at="2020-01-01T00:00:00Z")
        self.assertEqual(result["evidence"][0]["observed_at"], "2020-01-01T00:00:00Z")
        for stamp in (None, "2026-10-21", "2026-10-21T12:30:00", "2026-02-30T12:30:00Z",
                      "2026-10-21T12:30:00+15:00", "2026-10-21T12:30:00Z private"):
            with self.subTest(stamp=stamp):
                with self.assertRaises(PlaceEvidenceError):
                    project(observed_at=stamp)


if __name__ == "__main__":
    unittest.main()
