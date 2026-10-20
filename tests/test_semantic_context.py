from copy import deepcopy
from datetime import datetime, timezone
import math
import unittest
from unittest.mock import patch

from app.lab_context import ContextError, PUBLIC_ORIGINS, resolve_request, stage_request

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


def confirmed(lat=51.13, lon=71.43, **changes):
    value = {"origin": {"lat": lat, "lon": lon, "label": "Синтетический подтверждённый отель"},
             "origin_source": "google_places_text_search", "origin_id": None,
             "origin_reference": "https://www.google.com/maps/search/?api=1&query=test"}
    value.update(changes)
    return value


class SemanticContextTests(unittest.TestCase):
    def test_stage_unknown_named_start_never_uses_destination_or_gps(self):
        query = "Я у отеля Синтетический, хочу прогулку возле Mega 45 минут"
        payload = {"query": query, "geolocation": {"lat": 51.12, "lon": 71.42}, "location_status": "granted"}
        original = deepcopy(payload)
        with patch("socket.create_connection", side_effect=AssertionError("No network")):
            staged = stage_request(payload, now=NOW, start_quote="отеля Синтетический")
        self.assertTrue(staged["needs_place_resolution"])
        self.assertFalse(staged["needs_geolocation"])
        self.assertIsNone(staged["origin"])
        self.assertEqual(staged["unresolved_start_quote"], "отеля Синтетический")
        self.assertEqual(staged["resolved_context"]["origin_source"], "pending_place_resolution")
        self.assertEqual(staged["minutes"], 45)
        self.assertEqual(payload, original)

    def test_verified_origin_has_provenance_and_same_budget_clock_not_a_city_sample(self):
        query = "У меня 1,5 часа, я у отеля Синтетический, хочу природу"
        payload = {"query": query, "client_context": {"instant": "2026-10-21T09:00:00Z", "utc_offset_minutes": 300}}
        proof = confirmed()
        original = deepcopy(proof)
        result = resolve_request(payload, now=NOW, start_quote="отеля Синтетический", verified_start=proof)
        context = result["resolved_context"]
        self.assertEqual(context["minutes"], 90)
        self.assertEqual(context["origin"], proof["origin"])
        self.assertEqual(context["origin_source"], "google_places_text_search")
        self.assertEqual(context["origin_reference"], proof["origin_reference"])
        self.assertTrue(context["sensitive_origin"])
        self.assertFalse(context["is_city_sample"])
        self.assertIsNone(context["origin_id"])
        self.assertEqual(context["client_context"], payload["client_context"])
        self.assertEqual(proof, original)

    def test_model_location_quote_can_select_second_public_place_without_destination_confusion(self):
        query = "Я у Байтерека, хочу посетить Mega на час"
        result = resolve_request({"query": query}, now=NOW, start_quote="Байтерека")
        self.assertEqual(result["origin"], PUBLIC_ORIGINS["baiterek"])
        with self.assertRaises(ContextError):
            resolve_request({"query": query}, now=NOW, start_quote="Mega")

    def test_no_start_quote_does_not_treat_destination_as_origin(self):
        query = "Хочу посетить Mega и выпить кофе"
        result = stage_request({"query": query}, now=NOW, start_quote=None)
        self.assertTrue(result["needs_geolocation"])
        self.assertFalse(result["needs_place_resolution"])
        self.assertIsNone(result["origin"])
        with self.assertRaises(ContextError):
            resolve_request({"query": query, "location_status": "denied"}, now=NOW, start_quote=None)

    def test_known_public_and_catalog_start_still_use_local_data_without_resolution(self):
        result = stage_request({"query": "Я у EXPO, хочу прогулку 1–2 часа"}, now=NOW, start_quote="EXPO")
        self.assertFalse(result["needs_place_resolution"])
        self.assertFalse(result["needs_geolocation"])
        self.assertEqual(result["resolved_context"]["origin_id"], "expo")
        self.assertEqual(result["resolved_context"]["budget_options"], [60, 90, 120])
        catalog = {"places": [{"id": "fixture", "name": "Синтетический отель", "lat": 51.14, "lon": 71.44,
                               "source_url": "https://www.openstreetmap.org/node/1"}]}
        result = stage_request({"query": "Я у Синтетический отель, хочу кофе"}, now=NOW, catalog=catalog,
                               start_quote="Синтетический отель")
        self.assertEqual(result["resolved_context"]["origin_source"], "query_catalog")
        self.assertTrue(result["resolved_context"]["sensitive_origin"])
        self.assertEqual(result["origin"]["lat"], 51.14)

    def test_internal_proof_and_start_quote_cannot_be_injected_in_user_payload(self):
        for key, value in (("verified_start", confirmed()), ("start_quote", "Mega"), ("staging", True)):
            with self.subTest(key=key), self.assertRaises(ContextError) as caught:
                stage_request({"query": "Я у Mega, хочу кофе", key: value}, now=NOW)
            self.assertEqual(caught.exception.status, 400)
        for quote in (None, "Несуществующее место"):
            with self.subTest(quote=quote), self.assertRaises(ContextError):
                resolve_request({"query": "Я у отеля Синтетический, хочу кофе"}, now=NOW,
                                start_quote=quote, verified_start=confirmed())

    def test_verified_point_strict_geometry_and_source_identity_fail_closed(self):
        query = {"query": "Я у отеля Синтетический, хочу кофе"}
        bad = [confirmed(lat=float("nan")), confirmed(lon=float("inf")), confirmed(lat=True),
               confirmed(lat=50), confirmed(origin_id="mega"), confirmed(origin_source="guessed"),
               confirmed(origin_reference="http://www.google.com/maps/test"),
               confirmed(origin_reference="https://example.test/private"),
               confirmed(origin_reference="https://user:pass@www.google.com/maps"),
               {**confirmed(), "extra": True}]
        for proof in bad:
            with self.subTest(proof=proof), self.assertRaises(ContextError):
                resolve_request(query, now=NOW, start_quote="отеля Синтетический", verified_start=proof)

    def test_staging_still_rejects_malformed_time_gps_and_clock(self):
        cases = [{"query": "Я у отеля Синтетический, хочу кофе", "minutes": 9},
                 {"query": "Я у отеля Синтетический, хочу кофе", "geolocation": {"lat": float("nan"), "lon": 71.4}},
                 {"query": "Я у отеля Синтетический, хочу кофе",
                  "client_context": {"instant": "2020-01-01T00:00:00Z", "utc_offset_minutes": 300}}]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ContextError):
                stage_request(payload, now=NOW, start_quote="отеля Синтетический")
        with self.assertRaises(ContextError):
            stage_request({"query": "Я у отеля Синтетический на 481 минут"}, now=NOW,
                          start_quote="отеля Синтетический")

    def test_legacy_calls_keep_strict_unknown_place_behavior_and_response_shape(self):
        result = resolve_request({"query": "Я у Mega, хочу кофе на час"}, now=NOW)
        self.assertEqual(result["origin"], PUBLIC_ORIGINS["mega"])
        self.assertNotIn("needs_place_resolution", result)
        self.assertNotIn("unresolved_start_quote", result)
        with self.assertRaises(ContextError):
            resolve_request({"query": "Я у НеизвестныйОтель, хочу кофе", "geolocation": {"lat": 51.12, "lon": 71.42}},
                            now=NOW, probe=True)


if __name__ == "__main__":
    unittest.main()
