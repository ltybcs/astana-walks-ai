"""Own-stage amenity proof for dual-type candidates, with offline source stubs."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_unified_walks as fixtures
from app.ordered_goals import ordered_goals
from app.semantic_intent import validate_intent
from app.retrieval import matched_goal_indices


class OrderedStageAmenityTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnifiedTests.setUp(self)
        self.addCleanup(self.map.close)
        self.query = "Я у Байтерека, хочу кафе с розеткой, потом парк с Wi-Fi"
        self.original = validate_intent({
            "start_quote": "Байтерека", "goal_policy": "primary", "context": [], "excluded": [],
            "goals": [
                {"quote": "кафе", "search_text": "кафе", "category_hint": "cafe", "fulfilment": "venue_visit"},
                {"quote": "парк с Wi-Fi", "search_text": "парк", "category_hint": "park", "fulfilment": "venue_visit"}],
            "requirements": [
                {"quote": "розеткой", "key": "socket", "required": True, "goal_index": None},
                {"quote": "Wi-Fi", "key": "wifi", "required": True, "goal_index": None}],
        }, self.query)
        self.ordered, groups = ordered_goals(self.query, self.original)
        self.assertEqual(groups, ((0,), (1,)))
        self.assertEqual([(item.key, item.goal_index) for item in self.ordered.requirements],
                         [("socket", 0), ("wifi", None)])
        self.first = fixtures.poi("osm:node/70", "Синтетическое место двух типов", 51.1285, 71.4307, "cafe")
        self.first.update(source_categories=["park"], features={"socket": "unknown", "wifi": "yes"})
        self.second = fixtures.poi("osm:node/71", "Синтетическое место второй стадии", 51.129, 71.431, "park")
        self.dgis.search.return_value = {"places": [], "warnings": []}

    def recommend(self):
        self.osm.retrieve.return_value = {"selected": [{"poi": deepcopy(place)} for place in (self.first, self.second)]}
        # This is one independently validated semantic result, not a relaxed
        # parser or model call. The normal service still performs selection,
        # own-stage proof, route checks and output annotation itself.
        with patch("app.unified_walks.deterministic_plan", return_value=self.original):
            return self.service.recommend({"query": self.query, "time_of_day": "day", "consent": True})

    def test_known_negative_first_stage_socket_cannot_borrow_strict_second_stage_wifi_match(self):
        self.first["features"]["socket"] = "no"
        self.assertEqual(matched_goal_indices(self.first, self.ordered), (1,))
        result = self.recommend()
        for option in result["alternatives"]:
            self.assertFalse(option["route_built"])
            self.assertEqual(option["stops"], [])
            self.assertNotIn("amenity_fallback", option)
        self.narrator.assert_not_called()

    def test_each_unknown_is_labelled_for_the_actual_selected_stage(self):
        self.assertEqual(matched_goal_indices(self.first, self.ordered), (1,))
        result = self.recommend()
        for option in result["alternatives"]:
            self.assertTrue(option["route_built"])
            self.assertTrue(option["plan"]["ordered_goals_covered"])
            first, second = option["stops"]
            self.assertEqual([first["id"], second["id"]], [self.first["id"], self.second["id"]])
            self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["socket", "wifi"])
            self.assertEqual(first["unconfirmed_amenities"], ["socket"])
            self.assertEqual(second["unconfirmed_amenities"], ["wifi"])
            self.assertEqual(first["fulfilled_goal_indices"], [0])
            self.assertEqual(second["fulfilled_goal_indices"], [1])
            self.assertEqual(first["features"]["socket"], "unknown")
            self.assertIn("socket", first["requested_features"])
            self.assertNotIn("socket", second["requested_features"])

    def test_benign_strict_first_stage_sibling_does_not_gain_an_unknown_label(self):
        self.first["features"]["socket"] = "yes"
        result = self.recommend()
        for option in result["alternatives"]:
            self.assertTrue(option["route_built"])
            first, second = option["stops"]
            self.assertNotIn("unconfirmed_amenities", first)
            self.assertNotIn("amenity_note", first)
            self.assertEqual(second["unconfirmed_amenities"], ["wifi"])
            self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["wifi"])
            self.assertEqual(first["fulfilled_goal_indices"], [0])
            self.assertEqual(second["fulfilled_goal_indices"], [1])
            self.assertNotIn("socket", second["requested_features"])


if __name__ == "__main__":
    unittest.main()
