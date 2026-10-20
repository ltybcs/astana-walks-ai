"""Public landmark geometry and clock boundary regression; no network."""
from datetime import datetime, timezone
import unittest
from app.lab_context import resolve_request

class CategoryContextTests(unittest.TestCase):
    def resolve(self, query):
        return resolve_request({"query": query}, now=datetime(2026,10,20,6,tzinfo=timezone.utc))["resolved_context"]

    def test_new_public_landmarks_have_provenance_and_are_inside_supported_city(self):
        cases = (("Я около аэропорта, у меня 2 часа перед вылетом", "airport", "166500591"),
                 ("Еду с ребёнком в центральный парк, хочу музей", "central_park", "15957444"),
                 ("Необычная активность рядом с абу даби", "abu_dhabi", "762780007"))
        for query, identifier, source in cases:
            with self.subTest(identifier=identifier):
                context=self.resolve(query)
                self.assertEqual(context["origin_id"], identifier)
                self.assertIn(source, context["origin_reference"])
                self.assertFalse(context["sensitive_origin"])
                self.assertEqual(context["origin_source"], "query_landmark")
                self.assertTrue(51 <= context["origin"]["lat"] <=51.32)
                self.assertTrue(71.2 <= context["origin"]["lon"] <=71.7)
                if identifier == "airport":
                    self.assertEqual(context["budget_options"],[120])
                    self.assertTrue(any("T1" in text and "не подтверждён" in text for text in context["assumptions"]))

    def test_nightclub_is_a_venue_type_and_actual_night_request_is_preserved(self):
        for phrase in ("ночной клуб", "night club"):
            self.assertEqual(self.resolve("Хочу " + phrase + " у Байтерека")["time_of_day"], "day")
            self.assertEqual(self.resolve("Хочу " + phrase + " у Байтерека ночью")["time_of_day"], "night")

    def test_exact_cafeshka_glued_time_query_uses_named_park_without_gps(self):
        from app.activity_intent import parse_search_intent
        query="Мне срочно нужно в кафешку на 3 часаоколо Президентского парка"
        for extra in ({},{"geolocation":{"lat":55.7,"lon":37.6},"location_status":"granted"}):
            result=resolve_request({"query":query,**extra},probe=True)
            context=result["resolved_context"]
            self.assertFalse(result["needs_geolocation"])
            self.assertEqual(context["origin_id"],"presidential_park")
            self.assertEqual(context["budget_options"],[180])
            self.assertIn("112177946",context["origin_reference"])
            intent=parse_search_intent(result["query"])
            self.assertEqual(intent["categories"],["cafe"])
            self.assertTrue(intent["supported"])

    def test_new_known_landmark_still_wins_over_untrusted_gps(self):
        context=resolve_request({"query":"У Abu Dhabi Plaza хочу кино на час", "geolocation":{"lat":55.7,"lon":37.6}})["resolved_context"]
        self.assertEqual(context["origin_id"],"abu_dhabi")
        self.assertEqual(context["budget_options"],[60])
