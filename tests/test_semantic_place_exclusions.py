"""Named exclusions affect identity filtering, not every venue of that type."""
import unittest
from app.semantic_intent import deterministic_plan,validate_intent,IntentError

class SemanticNamedExclusionTests(unittest.TestCase):
    def test_named_botanical_garden_does_not_exclude_other_parks(self):
        query='Я у Mega, хочу парки на час. Не предлагай Ботанический сад'
        plan=deterministic_plan(query)
        self.assertEqual(plan.search_intent['categories'],['park'])
        self.assertEqual(plan.search_intent['excluded_categories'],[])
        value={'start_quote':'Mega','goals':[{'quote':'парки','search_text':'парки','category_hint':'park','fulfilment':'venue_visit'}],
               'goal_policy':'walk_mix','requirements':[],'context':[],
               'excluded':[{'quote':'Ботанический сад','category_hint':'park'}]}
        model=validate_intent(value,query)
        self.assertEqual(model.search_intent['categories'],['park'])
        self.assertEqual(model.search_intent['excluded_categories'],[])
        self.assertIsNone(model.excluded[0].category_hint)

    def test_uppercase_title_mask_preserves_generic_fallback_and_real_exclusions(self):
        query='Я около Условный mall, куда можно сходить до 2 часов? Не предлагай Парк Синтетический'
        plan=deterministic_plan(query)
        self.assertEqual(plan.search_intent['categories'],['walk'])
        self.assertEqual(plan.search_intent['excluded_categories'],[])
        query='Я у Mega, хочу парки на час. Не предлагай Парк Синтетический'
        self.assertEqual(deterministic_plan(query).search_intent['excluded_categories'],[])

    def test_actual_category_exclusion_is_not_masked(self):
        query='Я у Mega, хочу кафе, без парков'
        plan=deterministic_plan(query)
        self.assertEqual(plan.search_intent['categories'],['cafe'])
        self.assertIn('park',plan.search_intent['excluded_categories'])

    def test_named_exclusion_cannot_become_a_positive_goal(self):
        query='Я у Mega, хочу парки. Не предлагай Ботанический сад'
        value={'start_quote':'Mega','goals':[{'quote':'Ботанический сад','search_text':'парки','category_hint':'park','fulfilment':'venue_visit'}],
               'goal_policy':'walk_mix','requirements':[],'context':[],'excluded':[]}
        with self.assertRaises(IntentError):validate_intent(value,query)

if __name__=='__main__':unittest.main()
