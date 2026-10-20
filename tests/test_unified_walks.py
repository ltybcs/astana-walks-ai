"""Offline integration tests for the unified contract; no real providers."""
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from app.google_budget import GoogleError
from app.lab_context import ContextError, PUBLIC_ORIGINS, resolve_request
from app.route_map import RouteMap
from app.retrieval import Retriever
from app.unified_walks import UnifiedWalks


def poi(identifier, name, lat, lon, category="park"):
    return {"id": identifier, "name": name, "lat": lat, "lon": lon, "category": category,
            "description": "Место из синтетического тестового источника.", "features": {}, "opening_hours": None,
            "source_url": "https://www.openstreetmap.org/node/1" if identifier.startswith("osm") else "https://2gis.kz/astana/firm/1"}


class Google:
    def __init__(self):
        self._key = "BACKEND_TEST_KEY_NEVER_EXPOSE"
        self.budget = Mock()
        self.budget.status.return_value = {"state": "ready", "remaining": 500}
        self.search_count, self.legs, self.workflows = 0, [], []
        self.barrier = None
        self.fail_leg = False
    def status(self):
        return {"configured": True, "quota": self.budget.status()}
    @contextmanager
    def workflow(self, seconds):
        self.workflows.append(seconds)
        yield
    def search_places(self, parameters):
        self.search_count += 1
        if self.barrier:
            self.barrier.wait(timeout=3)
        return {"summary": "Полный синтетический ответ Google [0].", "places": [
            {"id": "google-mock", "place": "places/google-mock", "location": {"latitude": 51.092, "longitude": 71.410},
             "attribution": {"title": "Google Mock Park", "url": "https://www.google.com/maps/?cid=1"},
             "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/?cid=1"}}]}
    def compute_route(self, start, finish):
        self.legs.append((start, finish))
        if self.fail_leg:
            raise GoogleError("google_timeout", "Mock unavailable", 504)
        return {"routes": [{"duration": "120s", "distanceMeters": 120,
                            "attribution": {"title": "Google Maps", "url": "https://www.google.com/maps/"}}]}


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        self.google = Google()
        self.osm, self.dgis = Mock(), Mock()
        self.osm.retrieve.return_value = {"selected": [{"poi": poi("osm:node/1", "OSM Mock Park", 51.09, 71.409)}]}
        self.dgis.status.return_value = {"available": True, "configured": True}
        self.dgis.search.return_value = {"places": [poi("2gis:1", "DGIS Mock Park", 51.093, 71.411)], "warnings": []}
        def narrator(grounding, stops, *_):
            return {"text": "Цельный синтетический ответ на основе всех выбранных источников.",
                    "source_ids": [stop["source_id"] for stop in stops], "stop_ids": [stop["stop_id"] for stop in stops]}, "mock_validated"
        self.narrator = Mock(side_effect=narrator)
        self.map = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY")
        self.service = UnifiedWalks(resolve_request, self.osm, self.google, self.dgis, narrator=self.narrator,
                                    llm_config=object(), route_map=self.map)
        self.form = {"query": "Хочу прогулку возле Mega", "time_of_day": "day", "consent": True}

    def test_all_three_searches_overlap_and_one_answer_serves_three_budgets(self):
        barrier = self.google.barrier = Barrier(3)
        osm_value, dgis_value = self.osm.retrieve.return_value, self.dgis.search.return_value
        def local(*_, **_kwargs):
            barrier.wait(timeout=3)
            return osm_value
        def live(*_):
            barrier.wait(timeout=3)
            return dgis_value
        self.osm.retrieve.side_effect, self.dgis.search.side_effect = local, live
        result = self.service.recommend(self.form)
        self.assertEqual(self.google.search_count, 1)
        self.assertEqual(self.osm.retrieve.call_count, 1)
        self.assertEqual(self.dgis.search.call_count, 1)
        self.assertEqual(self.narrator.call_count, 1)
        self.assertEqual(self.google.workflows, [60])
        self.assertEqual(len(self.google.legs), 6)
        self.assertEqual([option["budget_minutes"] for option in result["alternatives"]], [60, 120, 240])
        self.assertEqual(set(result["answer"]["source_ids"]), {"o0", "g0", "d0"})
        self.assertEqual(result["mode"], "llm-grounded-unified")
        self.assertEqual(result["diagnostics"]["response_storage"], "request-memory-only")
        for option in result["alternatives"]:
            self.assertTrue(option["route_built"])
            self.assertEqual(option["budget_status"], "fits")
            self.assertEqual(len(option["stops"]), 3)
            plan = option["plan"]
            self.assertEqual(plan["total_minutes"], plan["walk_minutes"] + plan["visit_minutes"] + 5)
            self.assertLessEqual(plan["total_minutes"], option["budget_minutes"])
            self.assertEqual(plan["legs"][-1]["to_ref"], "origin")
            self.assertEqual(plan["legs"][0]["from_ref"], "origin")
            self.assertTrue(option["map_ticket"])
        self.assertEqual([receipt["selected_count"] for receipt in result["source_receipt"].values()], [1, 1, 1])

    def test_single_venue_retrieval_does_not_expand_to_secondary_architecture(self):
        origin = PUBLIC_ORIGINS["baiterek"]
        museum = poi("osm:node/1", "Синтетический музей", 51.1284, 71.4306, "museum")
        club = poi("osm:node/2", "Синтетический клуб", 51.129, 71.432, "computer_club")
        query = "Хочу в компьютерный клуб на пару часов рядом с Байтереком, интересует архитектура и туристическая прогулка"
        for has_club in (False, True):
            for needs_socket in (False, True):
                with self.subTest(has_club=has_club, needs_socket=needs_socket):
                    places = [museum, club] if has_club else [museum]
                    retriever = Retriever(data={"dataset": {"is_demo": True}, "places": deepcopy(places)})
                    result = retriever.retrieve(query + (" нужна розетка" if needs_socket else ""), origin, 120)
                    expected = [club["id"]] if has_club and not needs_socket else []
                    self.assertEqual([item["poi"]["id"] for item in result["selected"]], expected)
                    self.assertEqual(result["intent"]["activity_mode"], "single_venue")
                    self.assertTrue(all(result["intent"][flag] for flag in ("architecture", "tourist", "outdoor")))

    def test_single_venue_rechecks_adapter_categories_before_routing_or_model(self):
        self.addCleanup(self.map.close)
        museum = poi("osm:node/1", "Синтетический музей", 51.1284, 71.4306, "museum")
        club = poi("osm:node/2", "Синтетический клуб", 51.129, 71.432, "computer_club")
        self.dgis.search.return_value = {"places": [{**museum, "id": "2gis:synthetic"}], "warnings": []}
        query = "Хочу в компьютерный клуб на пару часов рядом с Байтереком, интересует архитектура и туристическая прогулка"
        for has_club, needs_socket in ((False, False), (True, False), (True, True)):
            with self.subTest(has_club=has_club, needs_socket=needs_socket):
                self.google.legs.clear()
                self.narrator.reset_mock()
                places = [museum, club] if has_club else [museum]
                self.osm.retrieve.return_value = {"selected": [{"poi": deepcopy(place)} for place in places]}
                result = self.service.recommend({**self.form, "minutes": 120,
                    "query": query + (" нужна розетка" if needs_socket else "")})
                eligible = has_club and not needs_socket
                self.assertEqual(result["source_receipt"]["osm"]["candidate_count"], int(eligible))
                self.assertEqual(result["source_receipt"]["dgis"]["candidate_count"], 0)
                self.assertEqual(result["source_receipt"]["google"]["candidate_count"], 0)
                self.assertEqual([stop["id"] for stop in result["alternatives"][0]["stops"]],
                                 [club["id"]] if eligible else [])
                self.assertEqual(len(self.google.legs), 2 if eligible else 0)
                self.assertEqual(self.narrator.call_count, int(eligible))
                if not eligible:
                    self.assertIsNone(result["answer"])
                    self.assertFalse(result["alternatives"][0]["route_built"])
                self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")
                self.assertEqual(len(result["grounding"]["sources"]), 1)

    def test_operator_status_contains_only_safe_metadata_and_never_repeats_search(self):
        self.service.recommend({**self.form, "query": "PRIVATE_QUERY_MARKER Хочу прогулку возле Mega"})
        counts = (self.google.search_count, self.dgis.search.call_count, self.narrator.call_count)
        metadata = self.service.status()["last_completed_workflow"]
        self.assertEqual(metadata["llm_status"], "mock_validated")
        self.assertEqual(metadata["mode"], "llm-grounded-unified")
        for value in ("PRIVATE_QUERY_MARKER", "51.0892", "71.4074", "OSM Mock Park", "DGIS Mock Park", "google-mock", "text", "source_url", "stop_id"):
            self.assertNotIn(value, repr(metadata))
        self.assertEqual(counts, (self.google.search_count, self.dgis.search.call_count, self.narrator.call_count))
        metadata["source_receipt"]["osm"]["candidate_count"] = 999
        self.assertNotEqual(self.service.status()["last_completed_workflow"]["source_receipt"]["osm"]["candidate_count"], 999)

    def test_unknown_denied_unresolved_and_outside_start_cost_nothing(self):
        for form in ({"query": "Хочу кофе"}, {"query": "Хочу кофе", "location_status": "denied"},
                     {"query": "45 минут возле гостиницы Несуществующая"},
                     {"query": "Хочу кофе", "geolocation": {"lat": 55.7, "lon": 37.6}}):
            with self.subTest(form=form), self.assertRaises(ContextError) as error:
                self.service.recommend({**form, "consent": True})
            self.assertEqual(error.exception.code, "known_start_required")
        self.google.budget.status.assert_not_called()
        self.osm.retrieve.assert_not_called()
        self.dgis.status.assert_not_called()
        self.dgis.search.assert_not_called()
        self.narrator.assert_not_called()
        self.assertEqual(self.google.search_count, 0)

    def test_unconfigured_or_exhausted_provider_blocks_search_and_model(self):
        self.dgis.status.return_value = {"available": False}
        with self.assertRaises(GoogleError):
            self.service.recommend(self.form)
        self.osm.retrieve.assert_not_called()
        self.narrator.assert_not_called()
        self.assertEqual(self.google.search_count, 0)

    def test_provider_failure_creates_no_misleading_two_source_model_answer(self):
        self.dgis.search.side_effect = RuntimeError("PRIVATE_PROVIDER_BODY")
        with self.assertRaises(GoogleError) as error:
            self.service.recommend(self.form)
        self.assertEqual(error.exception.code, "unified_provider_unavailable")
        self.assertNotIn("PRIVATE", str(error.exception))
        self.narrator.assert_not_called()

    def test_failed_directed_legs_do_not_create_map_or_call_model(self):
        self.google.fail_leg = True
        result = self.service.recommend(self.form)
        self.assertTrue(all(option["budget_status"] == "unverified" for option in result["alternatives"]))
        self.assertTrue(all(not option["route_built"] and option["map_ticket"] is None for option in result["alternatives"]))
        self.narrator.assert_not_called()
        self.assertIsNone(result["answer"])

    def test_empty_provider_is_reported_and_does_not_invent_stops(self):
        self.dgis.search.return_value = {"places": []}
        result = self.service.recommend(self.form)
        self.assertEqual(result["source_receipt"]["dgis"], {"status": "no_data", "candidate_count": 0, "selected_count": 0})
        self.assertFalse(any(stop["provider"] == "dgis" for stop in result["alternatives"][0]["stops"]))

    def test_google_start_itself_is_not_a_walking_stop_even_for_coffee(self):
        old_search = self.google.search_places
        def search(parameters):
            value = old_search(parameters)
            value["places"][0]["location"] = {"latitude": 51.0892, "longitude": 71.4074}
            return value
        self.google.search_places = search
        result = self.service.recommend({**self.form, "query": "Хочу кофе возле Mega"})
        self.assertEqual(result["source_receipt"]["google"]["candidate_count"], 0)
        self.assertEqual(len(result["grounding"]["sources"]), 1)
        self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")
        self.assertFalse(any(stop["provider"] == "google" for option in result["alternatives"] for stop in option["stops"]))

    def test_hard_unknown_properties_never_become_google_or_dgis_facts(self):
        self.osm.retrieve.return_value = {"selected": []}
        result = self.service.recommend({**self.form, "query": "Обязательно розетка возле Mega"})
        self.assertTrue(all(not option["stops"] for option in result["alternatives"]))
        self.narrator.assert_not_called()


    def test_unrouteable_or_too_long_first_candidate_does_not_poison_next_stops(self):
        origin = PUBLIC_ORIGINS["mega"]
        stops = [{**poi("synthetic:" + str(i), "Синтетическая остановка " + str(i), 51.09 + i / 1000, 71.41),
                  "stop_id": "test:" + str(i)} for i in range(3)]
        def point(value):
            return {"latLng": {"latitude": value["lat"], "longitude": value["lon"]}}
        for failure in ("outgoing", "return", "outgoing_too_long", "return_too_long", "invalid_duration"):
            with self.subTest(failure=failure):
                calls = []
                def route(start, finish):
                    calls.append((deepcopy(start), deepcopy(finish)))
                    first_outgoing = start == point(origin) and finish == point(stops[0])
                    first_return = start == point(stops[0]) and finish == point(origin)
                    if (failure == "outgoing" and first_outgoing) or (failure == "return" and first_return):
                        raise GoogleError("PRIVATE_RAW_CODE", "PRIVATE_PROVIDER_BODY", 502)
                    duration = "120s"
                    if (failure == "outgoing_too_long" and first_outgoing) or (failure == "return_too_long" and first_return):
                        duration = "12000s"
                    if failure == "invalid_duration" and first_outgoing:
                        duration = "unknown"
                    return {"routes": [{"duration": duration, "distanceMeters": 120,
                                        "attribution": {"title": "Synthetic Google Maps", "url": "https://www.google.com/maps/"}}]}
                self.google.compute_route = route
                before = deepcopy(stops)
                options = self.service._plans(stops, [30, 60], {}, origin)
                self.assertEqual(stops, before)
                self.assertEqual(len(calls), 6)
                # Skipping the first point must not move the route's origin.
                self.assertEqual(calls[2], (point(origin), point(stops[1])))
                self.assertEqual(calls[4], (point(stops[1]), point(stops[2])))
                for option in options:
                    self.assertEqual([stop["stop_id"] for stop in option["stops"]], ["test:1", "test:2"])
                    self.assertEqual(option["budget_status"], "fits")
                    self.assertTrue(option["route_built"])
                    plan = option["plan"]
                    self.assertLessEqual(plan["total_minutes"], option["budget_minutes"])
                    self.assertEqual(plan["walk_minutes"], 6)
                    self.assertEqual(plan["return_walk_minutes"], 2)
                    self.assertEqual(plan["legs"][0]["from_ref"], "origin")
                    self.assertEqual(plan["legs"][-1]["to_ref"], "origin")
                    checks = plan["route_checks"]
                    self.assertEqual([check["candidate_index"] for check in checks], [0, 1, 2])
                    self.assertFalse(checks[0]["fits_largest_budget"])
                    self.assertTrue(all(check["verified_legs"] and check["fits_largest_budget"] for check in checks[1:]))
                    expected = ("minimum_time_exceeds_largest_budget" if "too_long" in failure else
                                "route_duration_unverified" if failure == "invalid_duration" else "route_unavailable")
                    self.assertEqual(checks[0]["error_code"], expected)
                    self.assertNotIn("PRIVATE", repr(checks))
                    self.assertEqual(set(checks[0]), {"candidate_index", "outgoing_verified", "return_verified",
                                                     "verified_legs", "fits_largest_budget", "error_code"})
                self.map.close()

    def test_skipping_middle_candidate_keeps_previous_accepted_stop_and_prefix(self):
        origin = PUBLIC_ORIGINS["mega"]
        stops = [{**poi("synthetic:" + str(i), "Синтетическая точка", 51.09 + i / 1000, 71.41),
                  "stop_id": "test:" + str(i)} for i in range(3)]
        calls = []
        def route(start, finish):
            calls.append((start, finish))
            if finish["latLng"]["latitude"] == stops[1]["lat"]:
                raise GoogleError("google_timeout", "PRIVATE", 504)
            return {"routes": [{"duration": "120s", "distanceMeters": 120,
                                "attribution": {"title": "Synthetic Google Maps", "url": "https://www.google.com/maps/"}}]}
        self.google.compute_route = route
        option = self.service._plans(stops, [60], {}, origin)[0]
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[4][0]["latLng"]["latitude"], stops[0]["lat"])
        self.assertEqual([stop["stop_id"] for stop in option["stops"]], ["test:0", "test:2"])
        self.assertEqual(option["plan"]["walk_minutes"], 6)
        self.assertEqual(option["plan"]["legs"][1]["from_ref"], "test:0")
        self.assertEqual(option["plan"]["legs"][1]["to_ref"], "test:2")
        self.map.close()

    def test_asymmetric_outgoing_and_return_are_not_doubled_or_guessed(self):
        origin = PUBLIC_ORIGINS["mega"]
        stop = {**poi("synthetic:1", "Синтетическая точка", 51.09, 71.41), "stop_id": "test:1"}
        calls = []
        def route(start, finish):
            calls.append((start, finish))
            returning = finish["latLng"]["latitude"] == origin["lat"]
            return {"routes": [{"duration": "480s" if returning else "60s", "distanceMeters": 120,
                                "attribution": {"title": "Synthetic Google Maps", "url": "https://www.google.com/maps/"}}]}
        self.google.compute_route = route
        option = self.service._plans([stop], [35], {}, origin)[0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(option["budget_status"], "fits")
        self.assertEqual([leg["minutes"] for leg in option["plan"]["legs"]], [1, 8])
        self.assertEqual(option["plan"]["walk_minutes"], 9)
        self.assertEqual(option["plan"]["return_walk_minutes"], 8)
        self.assertEqual(option["plan"]["total_minutes"], 34)
        self.assertTrue(option["plan"]["route_checks"][0]["verified_legs"])
        self.map.close()


    def test_routing_preserves_google_ids_public_anchor_and_private_coordinate_start(self):
        self.addCleanup(self.map.close)
        cases = [(identifier, deepcopy(origin), False) for identifier, origin in PUBLIC_ORIGINS.items()]
        cases.append(("mega", {"lat": 51.088123, "lon": 71.405234}, True))
        for identifier, origin, sensitive in cases:
            for provider in ("google", "dgis"):
                with self.subTest(identifier=identifier, sensitive=sensitive, provider=provider):
                    self.google.legs.clear()
                    original = deepcopy(origin)
                    stop = {**poi("opaque-google-id" if provider == "google" else "2gis:synthetic",
                                  "Синтетическая точка", origin["lat"] + .001, origin["lon"] + .001),
                            "stop_id": provider + ":synthetic", "provider": provider}
                    context = {"origin_id": identifier, "sensitive_origin": sensitive}
                    option = self.service._plans([stop], [60], context, origin)[0]
                    anchor = {"latLng": {"latitude": origin["lat"], "longitude": origin["lon"]}}
                    waypoint = ({"id": stop["id"]} if provider == "google" else
                                {"latLng": {"latitude": stop["lat"], "longitude": stop["lon"]}})
                    self.assertEqual(self.google.legs, [(anchor, waypoint), (waypoint, anchor)])
                    self.assertEqual(option["budget_status"], "fits")
                    params = parse_qs(urlsplit(self.map.resolve({"ticket": option["map_ticket"]})["url"]).query)
                    self.assertEqual(params["origin"], [f"{origin['lat']:.7f},{origin['lon']:.7f}"])
                    self.assertEqual(params["origin"], params["destination"])
                    self.assertEqual(params["waypoints"], ["place_id:" + stop["id"] if provider == "google" else
                                                          f"{stop['lat']:.7f},{stop['lon']:.7f}"])
                    self.assertEqual(origin, original)


if __name__ == "__main__":
    unittest.main()
