"""Generic question recovery retains literal goals and strict start roles."""
import unittest

from app.lab_context import stage_request
from app.semantic_intent import IntentError, deterministic_plan, explicit_start_quote, validate_intent


def model_value(start, goal):
    return {"start_quote": start, "goals": [{"quote": goal, "search_text": "Места для прогулки",
            "category_hint": "walk", "fulfilment": "generic_walk"}], "excluded": [],
            "goal_policy": "walk_mix", "requirements": [], "context": []}


class GenericQuestionTests(unittest.TestCase):
    def test_arrival_question_has_literal_broad_goal_and_needs_gps_not_airport(self):
        query = "Я только что прилетел, что можно посмотреть в Астане"
        plan = deterministic_plan(query)
        self.assertEqual(plan.goals[0].quote, "что можно посмотреть")
        self.assertEqual(plan.search_intent["categories"], ["walk"])
        self.assertEqual(plan.goals[0].fulfilment, "generic_walk")
        self.assertEqual(plan.context, ())
        self.assertEqual(plan.requirements, ())
        self.assertIsNone(explicit_start_quote(query))
        draft = stage_request({"query": query})
        self.assertTrue(draft["needs_geolocation"])
        self.assertFalse(draft["needs_place_resolution"])
        self.assertIsNone(draft["origin"])
        self.assertEqual(draft["resolved_context"]["budget_options"], [60, 120, 240])

    def test_closed_question_forms_support_neutral_context_and_literal_provenance(self):
        for query, quote in (
            ("Что можно посмотреть в Астане?", "Что можно посмотреть"),
            ("Что посмотреть рядом со мной", "Что посмотреть"),
            ("Я недавно приехала в Астану. Что необычного можно увидеть?", "Что необычного можно увидеть"),
            ("Я сейчас в Астане, где можно погулять поблизости?", "погулять"),
            ("Мне скучно, у меня есть 1,5 часа. Куда можно пойти?", "Куда можно пойти"),
        ):
            with self.subTest(query=query):
                plan = deterministic_plan(query)
                self.assertEqual(plan.goals[0].quote, quote)
                self.assertIn(quote, query)
                self.assertEqual(plan.search_intent["categories"], ["walk"])
                self.assertFalse(plan.open_goal)
                self.assertEqual(plan.context, ())

    def test_numbered_departure_is_an_unresolved_name_not_device_gps_or_goal(self):
        for name in ("Камал-5", "Синтетический-7", "Kamal 5", "Условный Дом 17"):
            query = f"Мне скучно, у меня есть целый день. Куда можно пойти с {name}"
            with self.subTest(name=name):
                plan = deterministic_plan(query)
                self.assertEqual(plan.goals[0].quote, "Куда можно пойти")
                self.assertEqual(explicit_start_quote(query), name)
                draft = stage_request({"query": query}, start_quote=name)
                self.assertTrue(draft["needs_place_resolution"])
                self.assertFalse(draft["needs_geolocation"])
                self.assertEqual(draft["unresolved_start_quote"], name)
                self.assertIsNone(draft["origin"])
                self.assertNotIn(name, plan.search_text)
                semantic = validate_intent(model_value(name, "Куда можно пойти"), query)
                self.assertEqual(semantic.start_quote, name)
                self.assertNotIn(name, str(semantic.sanitized_narration))

    def test_departure_visit_verb_is_not_a_destination_role(self):
        query = "Куда можно сходить с Синтетический-7?"
        self.assertEqual(explicit_start_quote(query), "Синтетический-7")
        plan = validate_intent(model_value("Синтетический-7", "Куда можно сходить"), query)
        self.assertEqual(plan.start_quote, "Синтетический-7")
        self.assertEqual(deterministic_plan(query).goals[0].quote, "Куда можно сходить")

    def test_generic_question_cannot_rescue_unknown_goal_service_or_exclusion(self):
        for query in (
            "Хочу в планетарий. Что можно посмотреть в Астане?",
            "Стрельба, что можно посмотреть в Астане?",
            "Что можно посмотреть в планетарии Астаны?",
            "Что можно посмотреть, чтобы погладить капибару?",
            "Что можно посмотреть с VR-оборудованием?",
            "Не кафе, что можно посмотреть в Астане?",
            "Что можно посмотреть только с розетками?",
            "Что можно посмотреть в Астане и поиграть в Minecraft?",
            "Что можно посмотреть? Куда можно пойти?",
        ):
            with self.subTest(query=query), self.assertRaises(IntentError):
                deterministic_plan(query)

    def test_companions_devices_negation_and_destination_are_not_departure_names(self):
        for query in (
            "Куда можно пойти с ребёнком", "Куда можно пойти с друзьями",
            "Куда можно пойти с VR-2", "Куда можно пойти с RTX-4090",
            "Куда можно пойти не с Условный-7",
            "Не куда можно пойти с Условный-7",
            "Хочу посетить Условный-7", "Куда можно пойти в Условный-7",
        ):
            with self.subTest(query=query):
                self.assertIsNone(explicit_start_quote(query))
                with self.assertRaises(IntentError):
                    deterministic_plan(query)


if __name__ == "__main__":
    unittest.main()
