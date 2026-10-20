from urllib.parse import parse_qs, urlsplit
import unittest
from app.google_budget import GoogleError
from app.route_map import RouteMap


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
        ticket = service.issue({"lat": 51.0892, "lon": 71.4074},
                               [{"lat": 51.09125, "lon": 71.4128, "provider": "google", "id": "Google_mock"}],
                               context={"origin_id": "mega", "sensitive_origin": False})
        params = parse_qs(urlsplit(service.resolve({"ticket": ticket})["url"]).query)
        self.assertEqual(params["origin"], ["MEGA Silk Way, Astana, Kazakhstan"])
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


if __name__ == "__main__":
    unittest.main()
