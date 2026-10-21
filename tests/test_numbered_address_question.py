"""Literal generic sightseeing and numbered departure without address inference."""
import unittest
from app.lab_context import stage_request
from app.semantic_intent import IntentError,deterministic_plan,explicit_start_quote,validate_intent
from tests.test_generic_question import model_value

class NumberedAddressQuestionTests(unittest.TestCase):
    def test_lowercase_numbered_address_precedes_literal_question(self):
        for address in ('условная улица 17б','тестовый бульвар 28а','synthetic street 12b'):
            query=f'у меня есть целый день, с {address} какие есть интересные места?'
            with self.subTest(address=address):
                plan=deterministic_plan(query)
                self.assertEqual(plan.goals[0].quote,'какие есть интересные места')
                self.assertEqual(plan.search_intent['categories'],['walk'])
                self.assertEqual(explicit_start_quote(query),address)
                draft=stage_request({'query':query},start_quote=explicit_start_quote(query))
                self.assertTrue(draft['needs_place_resolution'])
                self.assertFalse(draft['needs_geolocation'])
                self.assertEqual(draft['unresolved_start_quote'],address)
                self.assertIsNone(draft['origin'])
                self.assertEqual(draft['resolved_context']['budget_options'],[240,360,480])
                semantic=validate_intent(model_value(address,'какие есть интересные места'),query)
                self.assertEqual(semantic.start_quote,address)
                self.assertNotIn(address,str(semantic.sanitized_narration))

    def test_numbered_address_is_not_companion_device_or_unknown_goal_rescue(self):
        for query in ('с друзьями 17 какие есть интересные места?',
                      'с VR 2 какие есть интересные места?',
                      'не с условного дома 17б какие есть интересные места?',
                      'хочу посетить условный дом 17б, какие есть интересные места?',
                      'с условного дома 17б какие есть интересные места чтобы погладить капибару?',
                      'с условного дома 17б какие есть интересные места только с Wi-Fi?',
                      'хочу планетарий, какие есть интересные места?'):
            with self.subTest(query=query),self.assertRaises(IntentError):deterministic_plan(query)
        for query in ('с друзьями 17 какие есть интересные места?',
                      'с VR 2 какие есть интересные места?',
                      'не с условного дома 17б какие есть интересные места?',
                      'хочу посетить условный дом 17б, какие есть интересные места?'):
            self.assertIsNone(explicit_start_quote(query))

    def test_pure_generic_question_without_start_still_requires_gps(self):
        query='Какие есть интересные места в Астане?'
        self.assertEqual(deterministic_plan(query).search_intent['categories'],['walk'])
        self.assertIsNone(explicit_start_quote(query))
        self.assertTrue(stage_request({'query':query})['needs_geolocation'])

if __name__=='__main__':unittest.main()
