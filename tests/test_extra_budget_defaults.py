"""Amount-less extra time is an explicit app interpretation, never total time."""
import unittest
from app.google_budget import GoogleError
from app.lab_context import stage_request,DEFAULT_BUDGETS
from app.route_request import validate_walking_request,attach_walking_baseline
from tests.test_point_to_point_preparation import route

class ExtraBudgetDefaultTests(unittest.TestCase):
    def test_explicit_amountless_extra_time_yields_labeled_default_options(self):
        for clause in ('есть дополнительное время','у меня есть доп время','есть лишнее время','additional time'):
            query='Хочу кофе от EXPO до Mega, '+clause
            with self.subTest(clause=clause):
                descriptor=validate_walking_request(query,[60,120,240])
                self.assertEqual(descriptor['budget_mode'],'extra')
                self.assertEqual(descriptor['extra_budget_options'],DEFAULT_BUDGETS)
                self.assertTrue(descriptor['extra_budget_default'])
                result=attach_walking_baseline(stage_request({'query':query},probe=True),descriptor,route(20))
                context=result['resolved_context']
                self.assertEqual(context['budget_options'],[80,140,260])
                self.assertEqual(context['extra_budget_options'],[60,120,240])
                self.assertEqual(result['minutes'],80)
                self.assertTrue(context['extra_budget_default'])
                self.assertTrue(any('сверх прямого пути' in note for note in context['assumptions']))
                self.assertNotIn('Время не указано; показаны варианты на 1, 2 и 4 часа.',context['assumptions'])

    def test_extra_adjective_without_time_noun_does_not_change_total_defaults(self):
        for clause in ('хочу дополнительный парк','есть дополнительный интерес','хочу кофе'):
            with self.subTest(clause=clause):
                query='От EXPO до Mega, '+clause
                descriptor=validate_walking_request(query,[60,120,240])
                self.assertEqual(descriptor['budget_mode'],'total')
                self.assertIsNone(descriptor['extra_budget_options'])
                result=attach_walking_baseline(stage_request({'query':query},probe=True),descriptor,route(20))
                self.assertEqual(result['resolved_context']['budget_options'],[60,120,240])

    def test_numeric_extra_and_conflicting_total_keep_previous_rules(self):
        query='Хочу кофе от EXPO до Mega, дополнительные 30 минут'
        descriptor=validate_walking_request(query,[30])
        self.assertEqual(descriptor['extra_budget_options'],[30])
        self.assertFalse(descriptor['extra_budget_default'])
        for clause in ('на час и дополнительные 30 минут','на час, есть доп время'):
            with self.subTest(clause=clause),self.assertRaises(GoogleError) as caught:
                validate_walking_request('Хочу кофе от EXPO до Mega '+clause,[60])
            self.assertEqual(caught.exception.code,'route_budget_ambiguous')

if __name__=='__main__':unittest.main()
