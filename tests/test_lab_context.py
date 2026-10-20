from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from app.lab_context import ContextError, PUBLIC_ORIGINS, resolve_request

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
BASE = "Хочу прогулку у Mega"


class LabContextTests(unittest.TestCase):
    def resolve(self, payload, **kwargs):
        return resolve_request(payload, now=NOW, **kwargs)

    def test_colloquial_durations_pasted_text_and_aliases_resolve_without_questions(self):
        cases = [("45мин", 45), ("на45минут", 45), ("час", 60), ("один час", 60),
                 ("полтора часа", 90), ("полторачаса", 90), ("час с половиной", 90),
                 ("1 час 30 минут", 90), ("1ч 15мин", 75), ("полчаса", 30), ("пол часа", 30),
                 ("1,5 часа", 90), ("восемь часов", 480), ("45\u200bминут", 45), ("45\u00a0минут", 45)]
        for duration, minutes in cases:
            for landmark in ("Меги", "MegaSilkWay", "МегаСилкВэй", "MEGA Silk Way"):
                with self.subTest(duration=duration, landmark=landmark):
                    context = self.resolve({"query": f"Время: {duration}.\nСтарт: «{landmark}».\nХочу гулять!"})["resolved_context"]
                    self.assertEqual(context["minutes"], minutes)
                    self.assertEqual(context["budget_options"], [minutes])
                    self.assertEqual(context["origin_id"], "mega")
                    self.assertEqual(context["minutes_source"], "query")
                    self.assertFalse(context["sensitive_origin"])

    def test_defaults_ranges_and_ambiguity_are_actual_bounded_options(self):
        for query, budgets, source in ((BASE, [60, 120, 240], "default_options"),
                                       (BASE + " на 1–2 часа", [60, 90, 120], "query_range"),
                                       (BASE + " на 30-60 минут", [30, 45, 60], "query_range"),
                                       (BASE + " 20 минут и кофе 30 минут", [60, 120, 240], "ambiguous_text_options")):
            with self.subTest(query=query):
                context = self.resolve({"query": query})["resolved_context"]
                self.assertEqual(context["budget_options"], budgets)
                self.assertEqual(context["minutes"], budgets[0])
                self.assertEqual(context["budget_source"], source)
                self.assertTrue(all(10 <= value <= 480 for value in budgets))
        context = self.resolve({"query": BASE + " на 1–2 часа"})["resolved_context"]
        self.assertEqual(context["requested_range"], [60, 120])

    def test_expo_has_its_own_public_anchor_and_year_is_not_time(self):
        for name in ("EXPO 2017", "ЭКСПО-2017", "экспо2017", "EXPO"):
            context = self.resolve({"query": f"Я у {name}, хочу гулять 1–2 часа"})["resolved_context"]
            self.assertEqual(context["origin_id"], "expo")
            self.assertEqual(context["origin"]["lat"], 51.09125)
            self.assertEqual(context["origin"]["lon"], 71.412806)
            self.assertNotEqual(context["origin"], PUBLIC_ORIGINS["mega"])
            self.assertEqual(context["budget_options"], [60, 90, 120])
            self.assertIn("Q1074528", context["origin_reference"])

    def test_probe_requests_geo_only_when_no_text_start_and_is_network_free(self):
        with patch("socket.create_connection", side_effect=AssertionError("No network")), patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            pending = self.resolve({"query": "Хочу необычную прогулку"}, probe=True)
            self.assertTrue(pending["needs_geolocation"])
            self.assertIsNone(pending["origin"])
            self.assertFalse(self.resolve({"query": BASE}, probe=True)["needs_geolocation"])
            denied = self.resolve({"query": "Хочу прогулку", "location_status": "denied"}, probe=True)
            self.assertFalse(denied["needs_geolocation"])
            self.assertTrue(denied["resolved_context"]["is_city_sample"])

    def test_generic_location_goals_need_geo_and_rejected_landmark_is_not_start(self):
        for query in ("Хочу час погулять у воды", "Хочу прогулку рядом с парком", "Хочу погулять у дома"):
            with self.subTest(query=query):
                result = self.resolve({"query": query}, probe=True)
                self.assertTrue(result["needs_geolocation"])
                self.assertIsNone(result["origin"])
        context = self.resolve({"query": "Хочу гулять не у Меги, а у Байтерека 45 минут"})["resolved_context"]
        self.assertEqual(context["origin_id"], "baiterek")
        self.assertEqual(context["budget_options"], [45])
        self.assertTrue(self.resolve({"query": "Хочу гулять не у Меги"}, probe=True)["needs_geolocation"])

    def test_text_start_wins_over_geo_and_stale_legacy_fields(self):
        payload = {"query": BASE + " 45 минут ночью", "minutes": 120, "origin_id": "river", "time_of_day": "day",
                   "geolocation": {"lat": 51.15, "lon": 71.5, "accuracy": 12}}
        before = deepcopy(payload)
        context = self.resolve(payload)["resolved_context"]
        self.assertEqual(context["origin_id"], "mega")
        self.assertEqual(context["minutes"], 45)
        self.assertEqual(context["time_of_day"], "night")
        self.assertTrue(context["assumptions"])
        self.assertFalse(context["sensitive_origin"])
        self.assertEqual(payload, before)

    def test_private_gps_and_out_of_area_never_become_silent_public_user_locations(self):
        payload = {"query": "Хочу прогулку", "geolocation": {"lat": 51.11, "lon": 71.42, "accuracy": 20}}
        context = self.resolve(payload)["resolved_context"]
        self.assertEqual(context["origin_source"], "browser_geolocation")
        self.assertTrue(context["sensitive_origin"])
        self.assertIsNone(context["origin_id"])
        self.assertEqual(context["origin"]["lat"], 51.11)
        payload["geolocation"] = {"lat": 52.1, "lon": 72.5}
        context = self.resolve(payload)["resolved_context"]
        self.assertTrue(context["is_city_sample"])
        self.assertTrue(context["sensitive_origin"])
        self.assertIn("Пример", context["origin"]["label"])
        self.assertTrue(context["assumptions"])

    def test_unknown_text_address_uses_labelled_sample_and_never_requests_gps(self):
        for payload in ({"query": "Я возле неизвестного отеля, хочу прогулку"},
                        {"query": "Я на улице Неизвестная 55, хочу прогулку", "geolocation": {"lat": 51.11, "lon": 71.42}}):
            result = self.resolve(payload, probe=True)
            context = result["resolved_context"]
            self.assertFalse(result["needs_geolocation"])
            self.assertTrue(context["is_city_sample"])
            self.assertTrue(context["sensitive_origin"])
            self.assertEqual(context["origin_source"], "city_sample")
            self.assertNotIn("Неизвестная", json.dumps(context, ensure_ascii=False))

    def test_catalog_name_and_exact_address_use_only_existing_geometry_and_are_sensitive(self):
        catalog = {"places": [{"id": "osm:node/1", "name": "Синтетическое кафе Альфа", "source_name": "Синтетическое кафе Альфа",
                               "lat": 51.12, "lon": 71.45, "source_url": "https://www.openstreetmap.org/node/1",
                               "source_tags": {"addr:street": "Тестовая", "addr:housenumber": "55"}}]}
        before = deepcopy(catalog)
        for query in ("Я у Синтетическое кафе Альфа, хочу прогулку", "Мой адрес Тестовая 55, хочу гулять"):
            context = self.resolve({"query": query}, catalog=catalog)["resolved_context"]
            self.assertEqual(context["origin_source"], "query_catalog")
            self.assertEqual(context["origin"]["lat"], 51.12)
            self.assertTrue(context["sensitive_origin"])
            self.assertIsNone(context["origin_id"])
        self.assertEqual(catalog, before)

    def test_snapshot_offset_and_typed_period_are_stable_and_clock_injected(self):
        client = {"instant": "2026-10-20T12:00:00Z", "utc_offset_minutes": 600}
        payload = {"query": BASE, "client_context": client}
        first = self.resolve(payload)["resolved_context"]
        second = resolve_request(payload, now=NOW + timedelta(minutes=5))["resolved_context"]
        self.assertEqual(first, second)
        self.assertEqual(first["time_of_day"], "night")
        self.assertEqual(first["time_of_day_source"], "client_current_time")
        self.assertEqual(first["client_context"], client)
        payload["query"] += " днем"
        self.assertEqual(self.resolve(payload)["time_of_day"], "day")
        fallback = resolve_request({"query": BASE}, now=NOW + timedelta(hours=5))["resolved_context"]
        self.assertEqual(fallback["time_of_day"], "night")
        self.assertEqual(fallback["time_of_day_source"], "astana_current_time")

    def test_malformed_data_and_unsupported_budgets_still_fail_before_planning(self):
        cases = [{"query": ""}, {"query": BASE, "minutes": True}, {"query": BASE, "minutes": 481},
                 {"query": BASE, "geolocation": {"lat": float("nan"), "lon": 71.4}},
                 {"query": BASE, "geolocation": {"lat": True, "lon": 71.4}},
                 {"query": BASE, "geolocation": {"lat": 51.1, "lon": 71.4, "accuracy": -1}},
                 {"query": BASE, "location_status": "unexpected"},
                 {"query": BASE, "client_context": {"instant": "2026-10-20T10:00:00Z", "utc_offset_minutes": 300}},
                 {"query": BASE, "client_context": {"instant": "2026-10-20T12:00:00+05:00", "utc_offset_minutes": 300}},
                 {"query": BASE, "client_context": {"instant": "2026-10-20T12:00:00Z", "utc_offset_minutes": True}},
                 {"query": BASE + " 9 минут"}, {"query": BASE + " 9 часов"}, {"query": BASE + " 60–30 минут"}]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ContextError) as caught:
                self.resolve(payload)
            self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
