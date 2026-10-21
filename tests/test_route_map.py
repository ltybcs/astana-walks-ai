from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
import unittest
from app.google_budget import GoogleError
from app.route_map import RouteMap
from app.lab_context import PUBLIC_ORIGINS


class RouteMapTests(unittest.TestCase):
    def test_only_built_ram_ticket_can_create_one_official_walking_route(self):
        service = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY")
        with self.assertRaises(GoogleError):
            service.resolve({"ticket": "arbitrary"})
        origin = {"lat": 51.0892, "lon": 71.4074}
        stops = [{"lat": 51.0901, "lon": 71.4102}, {"lat": 51.0913, "lon": 71.4114}]
        ticket = service.issue(origin, stops)
        self.assertNotIn("51.", ticket)
        result = service.resolve({"ticket": ticket})
        url = urlsplit(result["url"])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "www.google.com", "/maps/embed/v1/directions"))
        params = parse_qs(url.query)
        self.assertEqual(params["mode"], ["walking"])
        self.assertEqual(params["origin"], params["destination"])
        self.assertEqual(params["waypoints"], ["51.0901000,71.4102000|51.0913000,71.4114000"])
        self.assertEqual(len(service._routes), 1)

    def test_public_anchor_and_google_id_match_backend_route_semantics(self):
        service = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY")
        self.addCleanup(service.close)
        for identifier, origin in PUBLIC_ORIGINS.items():
            with self.subTest(identifier=identifier):
                ticket = service.issue(origin,
                                       [{"lat": 51.09125, "lon": 71.4128, "provider": "google", "id": "Google_mock"}],
                                       context={"origin_id": identifier, "sensitive_origin": False})
                params = parse_qs(urlsplit(service.resolve({"ticket": ticket})["url"]).query)
                self.assertEqual(params["origin"], [f"{origin['lat']:.7f},{origin['lon']:.7f}"])
                self.assertEqual(params["origin"], params["destination"])
                self.assertEqual(params["waypoints"], ["place_id:Google_mock"])

    def test_ticket_expiry_and_no_key_are_explicit(self):
        time = [0]
        service = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY", clock=lambda: time[0])
        ticket = service.issue({"lat": 51.1, "lon": 71.4}, [{"lat": 51.2, "lon": 71.4}])
        time[0] = 601
        with self.assertRaises(GoogleError) as error:
            service.resolve({"ticket": ticket})
        self.assertEqual(error.exception.code, "route_map_expired")
        service = RouteMap()
        with self.assertRaises(GoogleError) as error:
            service.resolve({"ticket": "unused"})
        self.assertEqual(error.exception.code, "route_map_unconfigured")


class PointToPointMapTests(unittest.TestCase):
    def setUp(self):
        self.service = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY")
        self.addCleanup(self.service.close)
        self.origin = {"lat": 51.0892, "lon": 71.4074, "label": "MEGA Silk Way"}
        self.destination = {"lat": 51.1283, "lon": 71.4305, "label": "Байтерек"}
        self.context = {"route_kind": "point_to_point", "origin": deepcopy(self.origin), "origin_id": "mega", "origin_source": "query_landmark",
                        "destination": deepcopy(self.destination), "destination_source": "query_landmark", "destination_id": "baiterek"}
        self.stop = {"lat": 51.0913, "lon": 71.4114}
        self.plan = {"route_kind": "point_to_point", "verified": True, "reaches_destination": True,
                     "returns_to_origin": False, "destination": deepcopy(self.destination),
                     "final_walk_minutes": 20, "direct_only": False}

    def params(self, ticket):
        return parse_qs(urlsplit(self.service.resolve({"ticket": ticket})["url"]).query)

    def test_point_to_point_ticket_preserves_exact_destination_and_stop_identity(self):
        stop = {**self.stop, "provider": "google", "id": "Google_mock"}
        ticket = self.service.issue(self.origin, [stop], context=self.context, plan=self.plan)
        params = self.params(ticket)
        self.assertEqual(params["origin"], ["51.0892000,71.4074000"])
        self.assertEqual(params["destination"], ["51.1283000,71.4305000"])
        self.assertNotEqual(params["origin"], params["destination"])
        self.assertEqual(params["waypoints"], ["place_id:Google_mock"])
        self.assertEqual(params["mode"], ["walking"])
        self.assertNotIn("51.", ticket)

    def test_verified_direct_only_ticket_has_no_fake_waypoint_or_return(self):
        self.plan["direct_only"] = True
        ticket = self.service.issue(self.origin, [], context=self.context, plan=self.plan)
        params = self.params(ticket)
        self.assertNotIn("waypoints", params)
        self.assertEqual(params["destination"], ["51.1283000,71.4305000"])
        self.assertNotEqual(params["origin"], params["destination"])

    def test_issued_ticket_freezes_endpoint_and_stop_values_in_memory(self):
        ticket = self.service.issue(self.origin, [self.stop], context=self.context, plan=self.plan)
        before = self.params(ticket)
        self.origin["lat"] = self.context["origin"]["lat"] = 51.2
        self.destination["lon"] = self.context["destination"]["lon"] = 71.6
        self.stop["lat"] = 51.2
        self.plan["destination"]["lat"] = 51.2
        self.assertEqual(self.params(ticket), before)

    def test_zero_stop_circle_is_still_invalid(self):
        for context in (None, {"route_kind": "round_trip"}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.service.issue(self.origin, [], context=context)

    def test_empty_point_to_point_requires_verified_direct_only_plan(self):
        for key, value in (("verified", False), ("reaches_destination", False),
                           ("returns_to_origin", True), ("route_kind", "round_trip"),
                           ("direct_only", False)):
            plan = {**self.plan, "direct_only": True, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.issue(self.origin, [], context=self.context, plan=plan)
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [], context=self.context)
        self.assertEqual(self.service._routes, {})

    def test_endpoint_mismatch_or_collapsed_endpoints_cannot_issue_ticket(self):
        cases = [
            ({**self.context, "origin": {**self.origin, "lat": 51.2}}, self.plan),
            ({**self.context, "destination": self.origin}, {**self.plan, "destination": self.origin}),
            (self.context, {**self.plan, "destination": {**self.destination, "lon": 71.5}}),
            ({**self.context, "destination": {**self.origin, "lat": self.origin["lat"] + 1e-9}},
             {**self.plan, "destination": {**self.origin, "lat": self.origin["lat"] + 1e-9}}),
        ]
        for context, plan in cases:
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.service.issue(self.origin, [self.stop], context=context, plan=plan)
        self.assertEqual(self.service._routes, {})

    def test_invalid_points_and_final_duration_cannot_issue_ticket(self):
        for destination in (None, {}, {**self.destination, "lat": True},
                            {**self.destination, "lon": float("nan")}, {**self.destination, "lon": 181}):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.service.issue(self.origin, [self.stop], context={**self.context, "destination": destination}, plan=self.plan)
        for minutes in (None, True, -1, float("nan"), float("inf")):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.service.issue(self.origin, [self.stop], context=self.context,
                                   plan={**self.plan, "final_walk_minutes": minutes})

    def test_current_point_to_point_bound_is_one_stop_and_direct_flag_is_consistent(self):
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [self.stop, self.stop], context=self.context, plan=self.plan)
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [self.stop], context=self.context,
                               plan={**self.plan, "direct_only": True})

    def test_point_to_point_cannot_silently_fall_back_to_circle(self):
        for context in ({**self.context, "route_kind": "driving"},
                        {**self.context, "route_kind": "round_trip"},
                        {"destination": self.destination}):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.service.issue(self.origin, [self.stop], context=context, plan=self.plan)
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [self.stop], plan=self.plan)

    def test_resolve_payload_cannot_override_server_destination(self):
        ticket = self.service.issue(self.origin, [self.stop], context=self.context, plan=self.plan)
        with self.assertRaises(GoogleError) as error:
            self.service.resolve({"ticket": ticket, "destination": self.origin})
        self.assertEqual(error.exception.code, "route_map_invalid")
        self.assertEqual(self.params(ticket)["destination"], ["51.1283000,71.4305000"])

    def test_direct_only_ticket_keeps_the_same_bounded_expiry(self):
        clock = [0]
        service = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY", clock=lambda: clock[0])
        self.addCleanup(service.close)
        ticket = service.issue(self.origin, [], context=self.context, plan={**self.plan, "direct_only": True})
        clock[0] = 601
        with self.assertRaises(GoogleError) as error:
            service.resolve({"ticket": ticket})
        self.assertEqual(error.exception.code, "route_map_expired")


class GeneralizedPointToPointMapTests(unittest.TestCase):
    setUp = PointToPointMapTests.setUp
    params = PointToPointMapTests.params

    def test_confirmed_named_destination_uses_exact_coordinate_not_a_second_place_id_location(self):
        context = {**self.context, "destination_source": "google_places_text_search", "destination_id": None,
                   "destination_place_id": "Google_destination_fixture", "destination_reference": "https://www.google.com/maps/place/fixture"}
        ticket = self.service.issue(self.origin, [self.stop], context=context, plan=self.plan)
        self.assertEqual(self.params(ticket)["destination"], ["51.1283000,71.4305000"])
        for patch in ({"destination_place_id": None}, {"destination_place_id": "../private"},
                      {"destination_reference": "javascript:bad"}, {"destination_source": "browser_geolocation"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.service.issue(self.origin, [self.stop], context={**context, **patch}, plan=self.plan)

    def test_two_stop_point_to_point_requires_closed_groups_and_each_own_goal_match(self):
        context = {**self.context, "ordered_goal_groups": [[0], [1, 2]]}
        stops = [{**self.stop, "stop_id": "one", "matched_goal_indices": [0]},
                 {"lat": 51.1, "lon": 71.42, "stop_id": "two", "matched_goal_indices": [2]}]
        plan = {**self.plan, "ordered_goals_covered": True}
        ticket = self.service.issue(self.origin, stops, context=context, plan=plan)
        self.assertEqual(self.params(ticket)["waypoints"], ["51.0913000,71.4114000|51.1000000,71.4200000"])
        for groups in ([[0], [0]], [[], [1]], [[0], [3]], [[True], [1]], [[0], [1], [2]]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.service.issue(self.origin, stops, context={**context, "ordered_goal_groups": groups}, plan=plan)
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, stops, context=context, plan={**plan, "ordered_goals_covered": False})
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [stops[1], stops[0]], context=context, plan=plan)


    def test_explicit_coordinate_origin_and_explicit_public_endpoint_keep_truthful_sources(self):
        context = {**self.context, "origin_source": "explicit_coordinates", "origin_id": None,
                   "destination_source": "explicit_landmark"}
        ticket = self.service.issue(self.origin, [self.stop], context=context, plan=self.plan)
        self.assertEqual(self.params(ticket)["origin"], ["51.0892000,71.4074000"])
        with self.assertRaises(ValueError):
            self.service.issue(self.origin, [self.stop], context={**context, "destination_id": "invented"}, plan=self.plan)
        outside = {**self.origin, "lat": 52.0}
        with self.assertRaises(ValueError):
            self.service.issue(outside, [self.stop], context={**context, "origin": outside}, plan=self.plan)


if __name__ == "__main__":
    unittest.main()
