"""Independent preparation regressions; clocks and provider responses are synthetic."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.google_budget import GoogleError
from app.google_client import GoogleClient
from app.lab_context import resolve_request, stage_request
from app.request_preparation import RequestPreparation, unique_place
from app.unified_walks import UnifiedWalks
from tests.test_preparation import Response, intent, place


class AddressIdentityReviewTests(unittest.TestCase):
    @staticmethod
    def address(number, identifier="candidate"):
        return {**place("Синтетическое здание", identifier),
                "address": "Астана, улица Тестовая " + number}

    def test_house_number_is_complete_not_a_token_inside_another_house(self):
        for query_number, wrong_number in (("7", "17"), ("7", "7-1"), ("7", "7/1"),
                                            ("7/1", "7"), ("7-1", "7"), ("7/1", "7-1")):
            with self.subTest(query=query_number, candidate=wrong_number):
                self.assertIsNone(unique_place([self.address(wrong_number)],
                                               "улица Тестовая " + query_number))

    def test_exact_number_wins_by_identity_even_when_a_wrong_hit_is_ranked_first(self):
        for number in ("7", "17", "7-1", "7/1"):
            with self.subTest(number=number):
                exact = self.address(number, "exact")
                wrong = self.address("27", "ranked-first")
                self.assertEqual(unique_place([wrong, exact], "улица Тестовая " + number), exact)
                self.assertEqual(unique_place([exact, wrong], "улица Тестовая " + number), exact)

    def test_two_distinct_exact_buildings_are_not_resolved_by_provider_rank(self):
        self.assertIsNone(unique_place([self.address("7", "one"), self.address("7", "two")],
                                       "улица Тестовая 7"))


class PlacesAndDeadlineReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.budget = Mock()
        self.budget.status.return_value = {"state": "ready", "used": 1, "remaining": 499}
        self.payload = {"places": [{"id": "synthetic-building",
            "displayName": {"text": "Синтетическое здание"},
            "formattedAddress": "Астана, улица Тестовая 7",
            "location": {"latitude": 51.12, "longitude": 71.43},
            "types": ["premise"], "googleMapsUri": "https://www.google.com/maps/?cid=1",
            "attributions": []}]}
        self.open = Mock(side_effect=lambda request, timeout: Response(deepcopy(self.payload)))
        self.client = GoogleClient("TEST_NOT_A_REAL_KEY", self.budget, http_open=self.open)

    def test_city_locality_district_and_whole_street_centroids_are_not_starts(self):
        for types in (["locality", "political"], ["country", "political"], ["route"],
                      ["administrative_area_level_1", "political"],
                      ["sublocality_level_2", "political"], []):
            with self.subTest(types=types):
                self.payload["places"][0]["types"] = types
                self.assertEqual(self.client.resolve_place("Тестовая 7"), [])
        # An actual specific building must survive the same geometry/name checks.
        self.payload["places"][0]["types"] = ["premise", "political"]
        self.assertEqual(self.client.resolve_place("Тестовая 7")[0]["id"], "synthetic-building")

    def test_wait_for_durable_quota_lock_is_subtracted_from_http_timeout(self):
        def reserve(_operation):
            self.now += 6
        self.budget.reserve.side_effect = reserve
        with patch("app.google_client.monotonic", side_effect=lambda: self.now):
            result = self.client.resolve_place("Тестовая 7", deadline=20)
        self.assertEqual(len(result), 1)
        self.budget.reserve.assert_called_once_with("places.resolve")
        self.open.assert_called_once()
        self.assertEqual(self.open.call_args.kwargs["timeout"], 4)

    def test_quota_wait_that_exhausts_deadline_never_opens_http(self):
        def reserve(_operation):
            self.now += 11
        self.budget.reserve.side_effect = reserve
        with patch("app.google_client.monotonic", side_effect=lambda: self.now):
            with self.assertRaises(GoogleError) as error:
                self.client.resolve_place("Тестовая 7", deadline=20)
        self.assertEqual(error.exception.code, "google_timeout")
        self.budget.reserve.assert_called_once_with("places.resolve")
        self.open.assert_not_called()


class ExpiredWorkflowReviewTests(unittest.TestCase):
    def setUp(self):
        self.form = {"query": "Хочу кофе возле Байтерека", "minutes": 120,
                     "time_of_day": "day", "consent": True}
        self.osm, self.google, self.dgis, self.narrator = Mock(), Mock(), Mock(), Mock()
        self.google.budget.status.return_value = {"state": "ready"}
        self.dgis.status.return_value = {"available": True}

    def test_expired_preparation_ticket_does_not_restart_inference_or_launch_sources(self):
        now = [10.0]
        gateway = Mock()
        gateway.extract.return_value = (intent(self.form["query"], "Байтерека"), "semantic", 1)
        preparation = RequestPreparation(resolve_request,
            lambda form, **kw: stage_request(form, probe=True, **kw),
            self.google, gateway, clock=lambda: now[0])
        token = preparation.prepare(self.form)["preparation_token"]
        now[0] += 201
        service = UnifiedWalks(resolve_request, self.osm, self.google, self.dgis,
                               preparation=preparation, narrator=self.narrator)
        with self.assertRaises(GoogleError) as error:
            service.recommend({**self.form, "preparation_token": token})
        self.assertEqual(error.exception.code, "invalid_preparation")
        gateway.extract.assert_called_once()
        self.google.resolve_place.assert_not_called()
        self.google.budget.status.assert_not_called()
        self.osm.retrieve.assert_not_called()
        self.dgis.status.assert_not_called()
        self.dgis.search.assert_not_called()
        self.google.search_places.assert_not_called()
        self.narrator.assert_not_called()

    def test_workflow_expiring_after_consume_but_before_search_never_launches_dgis(self):
        clean_form = {key: value for key, value in self.form.items() if key != "consent"}
        resolved = resolve_request(clean_form)
        # Consumption succeeded immediately before the deadline, but local setup
        # has now consumed the remaining reserve. Even a queued DGIS call is wrong.
        preparation = Mock()
        preparation.consume.return_value = SimpleNamespace(form=clean_form, resolved=resolved,
            intent_plan=None, deadline=502.0, intent_attempts=1, intent_status="semantic", place_attempts=0)
        service = UnifiedWalks(resolve_request, self.osm, self.google, self.dgis,
                               preparation=preparation, narrator=self.narrator)
        with patch("app.unified_walks.monotonic", return_value=500.0):
            with self.assertRaises(GoogleError) as error:
                service.recommend({**self.form, "preparation_token": "synthetic-opaque-token"})
        self.assertEqual(error.exception.code, "context_timeout")
        self.osm.retrieve.assert_not_called()
        self.dgis.search.assert_not_called()
        self.google.search_places.assert_not_called()
        self.narrator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
