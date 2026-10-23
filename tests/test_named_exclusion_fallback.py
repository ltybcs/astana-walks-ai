"""Bounded named exclusions do not erase ordinary UNKNOWN-amenity alternatives."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_unified_walks as fixtures
from app.lab_context import resolve_request
from app.place_exclusions import excluded_place_names, mask_place_exclusions
from app.semantic_intent import validate_intent


class NamedExclusionFallbackTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnifiedTests.setUp(self)
        self.addCleanup(self.map.close)
        self.alternative = fixtures.poi("osm:node/81", "Alternative Coffee", 51.09, 71.409, "cafe")
        self.named = fixtures.poi("osm:node/82", "Named Coffee", 51.0902, 71.4092, "cafe")
        self.named["features"] = {"socket": "yes"}
        self.dgis.search.return_value = {"places": [], "warnings": []}

    def recommend(self, quote, places):
        query = self.query = "Я у Mega, хочу кафе с розеткой. Не предлагай " + quote
        self.original = validate_intent({
            "start_quote": "Mega", "goal_policy": "primary", "context": [],
            "goals": [{"quote": "кафе", "search_text": "кафе", "category_hint": "cafe", "fulfilment": "venue_visit"}],
            "requirements": [{"quote": "розеткой", "key": "socket", "required": True, "goal_index": None}],
            "excluded": [{"quote": quote, "category_hint": None}],
        }, query)
        self.assertIsNone(self.original.excluded[0].category_hint)
        self.snapshot = deepcopy(self.original)
        form = {"query": query, "time_of_day": "day", "minutes": 60}
        prepared = SimpleNamespace(form=deepcopy(form), resolved=resolve_request(form),
            intent_plan=self.original, deadline=monotonic() + 120,
            intent_attempts=1, intent_status="validated_synthetic", place_attempts=0)
        self.service.preparation = Mock()
        self.service.preparation.consume.return_value = prepared
        self.osm.retrieve.return_value = {"selected": [{"poi": deepcopy(place)} for place in places]}
        result = self.service.recommend({**form, "consent": True})
        self.assertEqual(self.original, self.snapshot)
        self.assertEqual(prepared.form, form)
        return result

    def assert_unknown_socket_alternative(self, result):
        for option in result["alternatives"]:
            self.assertTrue(option["route_built"])
            self.assertEqual([stop["id"] for stop in option["stops"]], [self.alternative["id"]])
            self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["socket"])
            self.assertEqual(option["stops"][0]["unconfirmed_amenities"], ["socket"])
            self.assertEqual(option["stops"][0]["features"].get("socket", "unknown"), "unknown")

    def test_accepted_semantic_named_exclusion_keeps_unknown_socket_alternative(self):
        result = self.recommend("Named Coffee", [self.alternative])
        self.assertEqual(excluded_place_names(self.query), ("named coffee",))
        self.assert_unknown_socket_alternative(result)
        self.assertEqual(result["resolved_context"]["place_exclusions"]["matched_source_records"], 0)

    def test_arbitrary_unproved_exclusion_still_blocks_convenience_fallback(self):
        result = self.recommend("странные места", [self.alternative])
        self.assertEqual(mask_place_exclusions(self.query), self.query)
        # Broad literal identity filtering alone is not proof of a named venue.
        # This validated unknown prohibition must remain fail-closed.
        for option in result["alternatives"]:
            self.assertFalse(option["route_built"])
            self.assertEqual(option["stops"], [])
            self.assertNotIn("amenity_fallback", option)
        self.assertEqual(self.google.legs, [])
        self.narrator.assert_not_called()

    def test_named_strict_candidate_is_filtered_before_any_route_leg(self):
        result = self.recommend("Named Coffee", [self.named, self.alternative])
        self.assert_unknown_socket_alternative(result)
        forbidden = {"latLng": {"latitude": self.named["lat"], "longitude": self.named["lon"]}}
        self.assertTrue(self.google.legs)
        for start, finish in self.google.legs:
            self.assertNotEqual(start, forbidden)
            self.assertNotEqual(finish, forbidden)
        self.assertEqual(result["resolved_context"]["place_exclusions"]["matched_source_records"], 1)


if __name__ == "__main__":
    unittest.main()
