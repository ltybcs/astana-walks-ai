"""HTTP integration checks for new routes and the strict start boundary."""
import json
import unittest
from unittest.mock import Mock

from app.google_grounded import GoogleGrounded
from app.route_map import RouteMap
from tests import test_google_lab as lab_tests
running_server, request = lab_tests.running_server, lab_tests.request
from tests.test_unified_walks import Google, poi


class UnifiedHTTPTests(unittest.TestCase):
    setUp = lab_tests.GoogleLabTests.setUp
    write_fixture = lab_tests.GoogleLabTests.write_fixture

    def test_unknown_start_fails_before_configured_services_even_over_http(self):
        google_client, dgis = Google(), Mock()
        google = GoogleGrounded(google_client, lambda _: None)
        with running_server(self.root, google=google, dgis=dgis) as (server, _):
            for path in ("/api/unified/recommend", "/api/recommend", "/api/google/preview", "/api/google/recommend"):
                form = {"query": "Хочу прогулку", "location_status": "denied"}
                if path == "/api/unified/recommend":
                    form["consent"] = True
                if path == "/api/google/recommend":
                    form.update(consent=True, preview_token="invalid")
                status, headers, raw = request(server, path, "POST", json.dumps(form), {"Content-Type": "application/json"})
                self.assertEqual(status, 422, path)
                self.assertEqual(json.loads(raw)["code"], "known_start_required")
                self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(google_client.search_count, 0)
            google_client.budget.status.assert_not_called()
            dgis.status.assert_not_called()
            dgis.search.assert_not_called()

    def test_unified_route_and_map_have_one_source_flow_and_no_initial_map(self):
        client, dgis = Google(), Mock()
        google = GoogleGrounded(client, lambda _: None)
        dgis.status.return_value = {"available": True, "configured": True}
        dgis.search.return_value = {"places": [poi("2gis:1", "DGIS Mock Park", 51.093, 71.411)]}
        route_map = RouteMap("PUBLIC_EMBED_TEST_KEY_ONLY")
        with running_server(self.root, google=google, dgis=dgis, route_map=route_map) as (server, _):
            # No page GET can create a ticket or call providers.
            request(server, "/")
            self.assertEqual(client.search_count, 0)
            self.assertEqual(route_map._routes, {})
            form = {"query": "45 минут возле Mega хочу прогулку днём", "consent": True}
            status, _, raw = request(server, "/api/unified/recommend", "POST", json.dumps(form), {"Content-Type": "application/json"})
            self.assertEqual(status, 200, raw.decode())
            result = json.loads(raw)
            self.assertEqual(result["diagnostics"]["searches"], {"osm": 1, "google": 1, "dgis": 1})
            option = result["alternatives"][0]
            self.assertTrue(option["route_built"])
            status, _, raw = request(server, "/api/unified/map", "POST", json.dumps({"ticket": option["map_ticket"]}), {"Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["url"].startswith("https://www.google.com/maps/embed/v1/directions?"))
            self.assertEqual(client.search_count, 1)
            self.assertEqual(dgis.search.call_count, 1)
            status, _, raw = request(server, "/api/unified/map", "POST", '{"ticket":"invalid"}', {"Content-Type": "application/json", "Origin": "https://attacker.invalid"})
            self.assertEqual(status, 403)
            self.assertNotIn("PUBLIC_EMBED", raw.decode())


if __name__ == "__main__":
    unittest.main()
