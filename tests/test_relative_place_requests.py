"""Pure relative-visit grammar; no model, location or provider request."""
import unittest

from app.activity_intent import parse_search_intent
from app.lab_context import stage_request
from app.semantic_intent import IntentError, deterministic_plan


class RelativePlaceRequestTests(unittest.TestCase):
    def test_reported_park_request_reaches_120_minute_gps_preparation(self):
        query = "Дай парки в которые можно сходить ближайшие 2 часа рядом со мной"
        plan = deterministic_plan(query)
        self.assertEqual(plan.search_intent["categories"], ["park"])
        self.assertEqual(plan.requirements, ())
        draft = stage_request({"query": query})
        self.assertEqual(draft["minutes"], 120)
        self.assertEqual(draft["resolved_context"]["budget_options"], [120])
        self.assertTrue(draft["needs_geolocation"])
        self.assertFalse(draft["needs_place_resolution"])
        self.assertIsNone(draft["origin"])

    def test_relative_visit_is_shared_by_known_types_and_temporal_forms(self):
        cases = (
            ("Покажи музеи, которые можно посетить ближайший час рядом со мной", "museum"),
            ("Найди кафе, куда можно сходить на полтора часа поблизости", "cafe"),
            ("Хочу парк, в котором можно побывать в ближайшие 30 минут рядом со мной", "park"),
            ("Дай парки,\nв которые можно сходить в течение 1,5 часа рядом со мной", "park"),
            ("Парки, куда стоит сходить сегодня", "park"),
            ("Музеи,в которые можно сходить на 1.5 часа рядом со мной", "museum"),
        )
        for query, category in cases:
            with self.subTest(query=query):
                parsed = parse_search_intent(query)
                self.assertTrue(parsed["supported"])
                self.assertEqual(parsed["categories"], [category])
                self.assertEqual(deterministic_plan(query).search_intent["categories"], [category])

    def test_extra_activity_and_equipment_cannot_hide_in_relative_clause(self):
        for query in (
            "Дай парки в которые можно сходить пострелять ближайшие 2 часа рядом со мной",
            "Дай музеи, где можно погладить капибару",
            "Дай кафе, в которые можно сходить поиграть в Dota",
            "Дай парки, в которые можно сходить с VR-оборудованием",
            "Дай парки, в которые можно сходить, а потом в планетарий",
        ):
            with self.subTest(query=query):
                self.assertFalse(parse_search_intent(query)["supported"])
                with self.assertRaises(IntentError):
                    deterministic_plan(query)

    def test_unknown_or_separate_goal_is_not_rescued_by_known_place(self):
        for query in (
            "Дай планетарии, которые можно посетить ближайшие 2 часа рядом со мной",
            "Дай парки, в которые можно сходить на два часа. Хочу в планетарий",
            "Я рядом с парком, в который можно сходить, хочу в планетарий",
            "Не хочу парки, в которые можно сходить на час, хочу пострелять",
        ):
            with self.subTest(query=query), self.assertRaises(IntentError):
                deterministic_plan(query)

    def test_relative_grammar_does_not_remove_properties_or_exclusions(self):
        query = "Не музей, хочу тихое кафе с розеткой, куда можно сходить на час рядом со мной"
        plan = deterministic_plan(query)
        self.assertEqual(plan.search_intent["categories"], ["cafe"])
        self.assertEqual(plan.search_intent["excluded_categories"], ["museum"])
        self.assertEqual(set(plan.search_intent["preferences"]), {"quiet", "socket"})
        self.assertEqual({value.key for value in plan.requirements}, {"quiet", "socket"})


if __name__ == "__main__":
    unittest.main()
