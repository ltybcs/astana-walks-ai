"""Preparation/Places/gateway contract tests; providers are in-memory stubs."""
from contextlib import contextmanager
from copy import deepcopy
from email.message import Message
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.google_budget import GoogleBudget, GoogleError
from app.google_client import GoogleClient, json_bytes
from app.lab_context import ContextError, resolve_request, stage_request
from app.llm import LLMConfig
from app.model_gateway import ModelGateway, ModelUnavailable
from app.request_preparation import RequestPreparation, unique_place
from app.semantic_intent import IntentPlan, Goal


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json_bytes(value))
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'


def place(name='Керуен', identifier='p1', lat=51.126, lon=71.427):
    return {'id':identifier,'name':name,'address':'Астана, улица Достык 9',
            'lat':lat,'lon':lon,'source_url':'https://www.google.com/maps/?cid=1','attributions':[]}


def intent(query, start=None):
    return IntentPlan(start, (Goal('кофе', 'кафе', 'cafe', 'venue_visit'),), (), 'primary', (), ())


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.gateway, self.google = Mock(), Mock()
        self.query = 'Хочу кофе возле Керуена'
        self.gateway.extract.return_value = (intent(self.query,'Керуена'), 'semantic', 1)
        self.google.resolve_place.return_value = [place()]
        self.prep = RequestPreparation(resolve_request, lambda form,**kw:stage_request(form,probe=True,**kw),
                                       self.google,self.gateway,clock=lambda:self.now)
        self.form = {'query':self.query,'time_of_day':'day','consent':True}

    def test_explicit_unlisted_place_resolves_once_and_consumes_once(self):
        response = self.prep.prepare(self.form)
        token = response['preparation_token']
        self.assertNotIn(self.query,token)
        self.assertFalse(response['needs_geolocation'])
        final = self.prep.consume({**self.form,'preparation_token':token})
        self.assertEqual(final.resolved['origin']['lat'],51.126)
        self.assertEqual(final.resolved['resolved_context']['origin_source'],'google_places_text_search')
        self.assertEqual(self.gateway.extract.call_count,1)
        self.assertEqual(self.google.resolve_place.call_count,1)
        with self.assertRaises(GoogleError):
            self.prep.consume({**self.form,'preparation_token':token})

    def test_known_place_never_uses_google_resolution(self):
        self.form['query']='Хочу кофе возле Байтерека'
        self.gateway.extract.return_value=(intent(self.form['query'],'Байтерека'),'semantic',1)
        first=self.prep.prepare(self.form)
        final=self.prep.consume({**self.form,'preparation_token':first['preparation_token']})
        self.assertEqual(final.resolved['resolved_context']['origin_id'],'baiterek')
        self.google.resolve_place.assert_not_called()

    def test_gps_resume_reuses_intent_and_preserves_exact_origin(self):
        self.form['query']='Хочу кофе'
        self.gateway.extract.return_value=(intent(self.form['query']),'semantic',1)
        first=self.prep.prepare(self.form)
        self.assertTrue(first['needs_geolocation'])
        submitted={**self.form,'preparation_token':first['preparation_token'],
                   'location_status':'granted','geolocation':{'lat':51.12,'lon':71.43,'accuracy':35}}
        second=self.prep.prepare(submitted)
        submitted['preparation_token']=second['preparation_token']
        final=self.prep.consume(submitted)
        self.assertEqual(final.resolved['origin']['lat'],51.12)
        self.assertEqual(self.gateway.extract.call_count,1)
        self.google.resolve_place.assert_not_called()

    def test_missing_start_denied_gps_has_zero_external_calls(self):
        self.form.update(query='Хочу кофе',location_status='denied')
        self.gateway.extract.return_value=(intent(self.form['query']),'semantic',1)
        with self.assertRaises(ContextError):
            self.prep.prepare(self.form)
        self.google.resolve_place.assert_not_called()

    def test_ambiguous_empty_outside_or_wrong_name_never_choose_top_hit(self):
        for candidates in ([],[place('Сарыарка')],[place(identifier='a'),place(identifier='b')]):
            with self.subTest(candidates=len(candidates)):
                self.google.resolve_place.return_value=candidates
                with self.assertRaises(ContextError) as caught:
                    self.prep.prepare(self.form)
                self.assertEqual(caught.exception.code,'location_unresolved')

    def test_changed_query_gps_clock_replay_and_expiry_fail_without_reprepare(self):
        for change in ({'query':'Хочу чай возле Керуена'}, {'geolocation':{'lat':51.1,'lon':71.4}},
                       {'time_of_day':'night'}):
            first=self.prep.prepare(self.form)
            attempts=self.gateway.extract.call_count
            with self.assertRaises(GoogleError):
                self.prep.consume({**self.form,'preparation_token':first['preparation_token'],**change})
            self.assertEqual(self.gateway.extract.call_count,attempts)
        first=self.prep.prepare(self.form)
        self.now +=201
        with self.assertRaises(GoogleError):
            self.prep.consume({**self.form,'preparation_token':first['preparation_token']})

    def test_public_name_transliteration_does_not_require_an_alias_catalog(self):
        candidate=place('Keruen')
        self.assertIs(unique_place([candidate], 'Керуена'),candidate)
        candidate=place('Kimchi')
        self.assertIs(unique_place([candidate], 'Кимчи'),candidate)
        self.assertIsNone(unique_place([candidate], '7'))
        self.assertIsNone(unique_place([place('Алаудин')], 'отель Алау'))
        self.assertIsNone(unique_place([place('HiltonGarden')], 'Hilton'))

    def test_json_key_order_is_not_a_change_to_the_request(self):
        first=self.prep.prepare(self.form)
        reordered=dict(reversed(list(self.form.items())))
        reordered['preparation_token']=first['preparation_token']
        self.assertEqual(self.prep.consume(reordered).resolved['origin']['lat'],51.126)

    def test_invalid_structure_or_missing_consent_never_calls_model_or_places(self):
        for form in ({'query':self.query}, {'query':self.query,'consent':False},
                     {**self.form,'query':''},{**self.form,'unexpected':True},
                     {**self.form,'geolocation':{'lat':float('nan'),'lon':71.4}}):
            self.gateway.reset_mock();self.google.reset_mock()
            with self.assertRaises((GoogleError,ContextError)):
                self.prep.prepare(form)
            self.gateway.extract.assert_not_called();self.google.resolve_place.assert_not_called()


class PlacesResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'budget.json';GoogleBudget.initialize(path)
        self.budget=GoogleBudget(path)
        self.value={'places':[{'id':'public-place','displayName':{'text':'Керуен'},
            'formattedAddress':'Астана, Достык 9','location':{'latitude':51.126,'longitude':71.427},
            'types':['shopping_mall'],'googleMapsUri':'https://www.google.com/maps/?cid=1','attributions':[]}]}
        self.requests=[]
        def http(request,timeout):
            self.requests.append((request,timeout));return Response(self.value)
        self.client=GoogleClient('TEST_NOT_A_REAL_KEY',self.budget,http_open=http)

    def test_fixed_mask_rectangle_and_one_shared_budget_reservation(self):
        result=self.client.resolve_place('Керуена')
        self.assertEqual(len(result),1)
        request,timeout=self.requests[0]
        self.assertEqual(request.full_url,'https://places.googleapis.com/v1/places:searchText')
        self.assertLessEqual(timeout,15)
        self.assertNotIn('*',request.get_header('X-goog-fieldmask'))
        import json
        body=json.loads(request.data)
        self.assertEqual(body['pageSize'],3)
        self.assertIn('rectangle',body['locationRestriction'])
        self.assertEqual(self.budget.status()['used'],1)
        self.assertEqual(self.client.resolve_place('Керуена')[0]['name'],'Керуен')
        self.assertEqual(self.budget.status()['used'],2)

    def test_malformed_payload_does_not_expose_or_accept_provider_content(self):
        for mutation in ({'location':{'latitude':True,'longitude':71.4}},
                         {'googleMapsUri':'javascript:alert(1)'},{'id':'../private'},
                         {'rating':5}):
            original=deepcopy(self.value)
            self.value['places'][0].update(mutation)
            with self.assertRaises(GoogleError) as error:self.client.resolve_place('Керуена')
            self.assertEqual(error.exception.code,'google_protocol_error')
            self.assertNotIn('TEST_NOT_A_REAL_KEY',str(error.exception))
            self.value=original

    def test_outside_city_is_not_returned_and_invalid_query_is_not_charged(self):
        self.value['places'][0]['location']['latitude']=50.1
        self.assertEqual(self.client.resolve_place('Керуена'),[])
        before=self.budget.status()['used']
        for name in ('','https://bad.example','x'*201):
            with self.assertRaises(GoogleError):self.client.resolve_place(name)
        self.assertEqual(self.budget.status()['used'],before)


class GatewayTests(unittest.TestCase):
    def test_loopback_only_active_session_and_bounded_response(self):
        from urllib.request import Request
        calls=[]
        gateway=ModelGateway(LLMConfig('http://127.0.0.1:8081/v1','mock','',2),
                             http_open=lambda req,timeout:(calls.append(timeout) or Response({'ok':True})))
        request=Request('http://127.0.0.1:8081/v1/chat/completions')
        with self.assertRaises(ModelUnavailable):gateway.open(request,2)
        import time
        with gateway.session(time.monotonic()+2,seconds=1):
            with gateway.open(request,2) as response:self.assertIn(b'ok',response.read(100))
            with self.assertRaises(ModelUnavailable):gateway.open(Request('https://example.com/'),2)
        self.assertEqual(len(calls),1)
        self.assertLessEqual(calls[0],1)

    def test_unconfigured_model_never_performs_http(self):
        opener=Mock()
        gateway=ModelGateway(None,http_open=opener)
        self.assertEqual(gateway.extract('Хочу кофе',100),(None,'unconfigured',0))
        opener.assert_not_called()


if __name__=='__main__':unittest.main()
