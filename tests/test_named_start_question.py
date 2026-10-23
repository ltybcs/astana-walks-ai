"""A named current-place frame plus a bounded generic visit question."""
from contextlib import nullcontext
import unittest
from unittest.mock import Mock
from app.lab_context import resolve_request,stage_request
from app.request_preparation import RequestPreparation,unique_place
from app.semantic_intent import deterministic_plan,explicit_start_quote,IntentError

class NamedStartQuestionTests(unittest.TestCase):
    def test_literal_mall_frame_and_up_to_budget_survive_invalid_model_format(self):
        query='Я около Условный mall, куда можно сходить до 2 часов?'
        plan=deterministic_plan(query)
        self.assertEqual(plan.goals[0].quote,'куда можно сходить')
        self.assertEqual(plan.search_intent['categories'],['walk'])
        self.assertEqual(explicit_start_quote(query),'условный mall')
        gateway=Mock();gateway.extract.return_value=(None,'content_json',1)
        google=Mock();google.resolve_place.return_value=[{'id':'public-synthetic','name':'ТРЦ "Условный"',
            'address':'Астана','lat':51.13,'lon':71.44,'source_url':'https://www.google.com/maps/?cid=1','attributions':[]}]
        prep=RequestPreparation(resolve_request,lambda form,**kw:stage_request(form,probe=True,**kw),google,gateway)
        form={'query':query,'consent':True}
        first=prep.prepare(form)
        final=prep.consume({**form,'preparation_token':first['preparation_token']})
        self.assertFalse(first['needs_geolocation'])
        self.assertEqual(final.resolved['resolved_context']['budget_options'],[120])
        self.assertEqual(final.resolved['resolved_context']['origin_source'],'google_places_text_search')
        google.resolve_place.assert_called_once();gateway.extract.assert_called_once()
        google.compute_route.assert_not_called()

    def test_english_generic_mall_classifier_is_not_identity_or_ambiguity_proof(self):
        candidate={'id':'first','name':'ТРЦ "Условный"','address':'Астана'}
        for query in ('Условный mall','Условный malls','Условный shopping center',
                      'Условный shopping centre'):
            with self.subTest(query=query):
                self.assertIs(unique_place([candidate],query),candidate)
                self.assertIsNone(unique_place([candidate,{**candidate,'id':'second'}],query))
                self.assertIsNone(unique_place([{**candidate,'name':'ТРЦ "Другой"'}],query))
        for query in ('mall','malls','shopping center','shopping centre','ТЦ','ТРЦ'):
            with self.subTest(generic=query):self.assertIsNone(unique_place([candidate],query))

    def test_named_frame_does_not_rescue_extra_service_unknown_goal_or_destination(self):
        for query in ('Я около Условный mall, куда можно сходить с VR?',
                      'Хочу планетарий. Я около Условный mall, куда можно сходить до 2 часов?',
                      'Я около Условный mall, куда можно сходить чтобы погладить капибару?',
                      'Я не около Условный mall, куда можно сходить до 2 часов?',
                      'Хочу посетить Условный mall, куда можно сходить до 2 часов?'):
            with self.subTest(query=query),self.assertRaises(IntentError):deterministic_plan(query)

if __name__=='__main__':unittest.main()
