"""Confirmed arbitrary endpoints, extra budgets and one-use GPS continuation."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock
from app.google_budget import GoogleError
from app.lab_context import ContextError,PUBLIC_ORIGINS,resolve_request,stage_request
from app.request_preparation import RequestPreparation
from app.route_request import validate_walking_request,walking_bindings,walking_waypoints
from app.semantic_intent import deterministic_plan
from tests.test_point_to_point_preparation import route


def candidate(name,identifier='place-a',lat=51.13,lon=71.44):
    return {'name':name,'id':identifier,'address':'Астана, '+name,'lat':lat,'lon':lon,
            'source_url':'https://www.google.com/maps/?cid=1','attributions':[]}


class GeneralWalkingPreparationTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.events=[]
        self.google=Mock();self.google.workflow.side_effect=lambda seconds:nullcontext()
        self.google.compute_route.side_effect=lambda a,b:(self.events.append('walk') or route(20))
        self.google.resolve_place.side_effect=lambda name,**kw:(self.events.append('place') or
            [candidate(name,'p-'+str(len(self.events)),51.13+len(self.events)/10000,71.44)])
        self.gateway=Mock()
        self.gateway.extract.side_effect=lambda query,deadline:(self.events.append('intent') or
            (deterministic_plan('Хочу кофе'),'semantic',1))
        self.prep=RequestPreparation(resolve_request,lambda form,**kw:stage_request(form,probe=True,**kw),
            self.google,self.gateway,clock=lambda:self.now)
        self.form={'query':'Хочу кофе от условная 17б до тестовая 28а на час',
                   'consent':True,'time_of_day':'day'}

    def test_two_named_addresses_verified_before_baseline_and_model_with_bound_identity(self):
        final=self.prep.consume(self.form)
        self.assertEqual(self.events,['place','place','walk','intent'])
        self.assertEqual(final.place_attempts,2)
        context=final.resolved['resolved_context'];first,last=walking_bindings(context)
        self.assertIsNone(context['origin_id']);self.assertIsNone(context['destination_id'])
        self.assertEqual(first['source'],'google_places_text_search')
        self.assertNotEqual(first['id'],last['id'])
        self.assertEqual(final.resolved['route_baseline']['origin_binding'],first)
        self.assertEqual(final.resolved['route_baseline']['destination_binding'],last)
        self.assertTrue(context['sensitive_origin'])
        a,b=self.google.compute_route.call_args.args
        self.assertEqual((a,b),walking_waypoints(context))
        self.assertIn('latLng',a);self.assertNotIn('id',a);self.assertNotIn('address',b)

    def test_mixed_public_and_named_endpoints_do_not_relookup_public_identity(self):
        for query,key in (('Хочу кофе от Mega до условная 17б на час','origin'),
                          ('Хочу кофе от условная 17б до Mega на час','destination')):
            with self.subTest(query=query):
                self.events.clear();self.google.resolve_place.reset_mock()
                final=self.prep.consume({**self.form,'query':query})
                self.assertEqual(self.google.resolve_place.call_count,1)
                self.assertEqual(final.resolved['resolved_context'][key],PUBLIC_ORIGINS['mega'])
                self.assertEqual(self.events,['place','walk','intent'])

    def test_common_street_abbreviation_and_house_number_are_literal_endpoints(self):
        final=self.prep.consume({**self.form,'query':'Хочу кофе от ул. условная 17б до тестовая, 28а на час'})
        self.assertEqual(self.google.resolve_place.call_count,2)
        self.assertEqual(self.google.resolve_place.call_args_list[0].args[0],'ул. условная 17б')
        self.assertEqual(self.google.resolve_place.call_args_list[1].args[0],'тестовая, 28а')
        self.assertEqual(final.resolved['resolved_context']['route_kind'],'point_to_point')

    def test_unavailable_lookup_or_invalid_first_point_never_looks_up_second(self):
        for value in ([candidate('условная 17б',lat=49.0)],OSError('PRIVATE')):
            with self.subTest(value=type(value).__name__):
                self.google.resolve_place.reset_mock()
                self.google.resolve_place.side_effect=value if isinstance(value,Exception) else None
                self.google.resolve_place.return_value=value
                with self.assertRaises(GoogleError) as caught:self.prep.prepare(self.form)
                self.assertEqual(self.google.resolve_place.call_count,1)
                self.assertNotIn('PRIVATE',caught.exception.message)
                self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()

    def test_missing_ambiguous_outside_or_wrong_sourceproof_stops_before_route_and_model(self):
        for values in ([],[candidate('другое место')],
                       [candidate('условная 17б','p1'),candidate('условная 17б','p2')],
                       [candidate('условная 17б',lat=50.9)],
                       [{**candidate('условная 17б'),'source_url':'https://invalid.example/'}],
                       [{**candidate('условная 17б'),'id':'../bad'}]):
            with self.subTest(values=values):
                self.google.resolve_place.side_effect=None;self.google.resolve_place.return_value=values
                with self.assertRaises(GoogleError) as caught:self.prep.prepare(self.form)
                self.assertEqual(caught.exception.code,'route_endpoints_unresolved')
                self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()

    def test_bare_home_work_are_not_geocoded_or_guessed(self):
        for query in ('Хочу кофе от работы до дома на час','Walk from home to work',
                      'Хочу кофе от моего дома до Mega на час'):
            with self.subTest(query=query),self.assertRaises(GoogleError) as caught:
                self.prep.prepare({**self.form,'query':query})
            self.assertEqual(caught.exception.code,'route_endpoints_unresolved')
        self.google.resolve_place.assert_not_called();self.google.compute_route.assert_not_called()
        self.gateway.extract.assert_not_called()

    def test_recovered_current_work_home_roles_never_reach_places(self):
        for query in ('Я на работе, хочу пешком до Mega на час',
                      'Я у работы, хочу пешком до Mega на час',
                      'Я в офисе, хочу пешком до Mega на час',
                      'Я у дома, хочу пешком до Mega на час'):
            with self.subTest(query=query):
                try:response=self.prep.prepare({**self.form,'query':query})
                except GoogleError as error:self.assertEqual(error.code,'route_endpoints_unresolved')
                else:self.assertTrue(response['needs_geolocation'])
                self.google.resolve_place.assert_not_called();self.google.compute_route.assert_not_called()
                self.gateway.extract.assert_not_called()

    def test_current_named_start_still_resolves_before_route_without_null_quote_error(self):
        query='Я у условная 17б, хочу пешком до Mega на час'
        final=self.prep.consume({**self.form,'query':query})
        self.assertEqual(self.events,['place','walk','intent'])
        self.assertEqual(final.resolved['resolved_context']['origin_source'],'google_places_text_search')
        self.assertEqual(final.resolved['resolved_context']['destination_id'],'mega')

    def test_accompaniment_amenity_and_temporal_tails_are_not_routes(self):
        for query in ('Хочу кафе с розетками до18:00 возлеMega',
                      'Хочу кафе с розетками до 18:00 возле Mega',
                      'Хочу с ребёнком гулять до вечера уMega',
                      'Хочу кофе с собой до двух часов рядом сMega',
                      'Хочу с другом до Mega пешком',
                      'Хочу с моей подругой гулять до вечера возле Mega'):
            with self.subTest(query=query):self.assertIsNone(validate_walking_request(query,[120]))

    def test_linked_transport_suffix_rejects_without_confusing_story_or_without_car(self):
        for query in ('От EXPO до Mega, на такси','От EXPO до Mega. На машине',
                      'От EXPO до Mega; by bus'):
            with self.subTest(query=query),self.assertRaises(GoogleError) as caught:
                self.prep.prepare({**self.form,'query':query})
            self.assertEqual(caught.exception.code,'unsupported_route_mode')
        self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()
        for query in ('Хочу кофе от EXPO до Mega на час, без машины',
                      'Приехал на такси, хочу кофе от EXPO до Mega на час',
                      'Хочу кофе от EXPO до Mega на час. На такси приехал вчера'):
            with self.subTest(query=query):
                final=self.prep.consume({**self.form,'query':query})
                self.assertEqual(final.resolved['resolved_context']['route_kind'],'point_to_point')
        descriptor=validate_walking_request('От кафе Друг до Mega',[60])
        self.assertEqual(descriptor['origin_spec']['kind'],'named')

    def test_malformed_original_fields_keep_400_before_any_external_work(self):
        queries=(self.form['query'],'Хочу кофе от EXPO до Mega на час')
        for query in queries:
            for change in ({'geolocation':{'lat':float('nan'),'lon':71.4}},
                           {'origin':{'lat':51.1,'lon':71.4}},
                           {'client_context':{'instant':'invalid','utc_offset_minutes':300}}):
                with self.subTest(query=query,change=change),self.assertRaises(ContextError) as caught:
                    self.prep.prepare({**self.form,'query':query,**change})
                self.assertEqual(caught.exception.status,400)
                self.google.resolve_place.assert_not_called();self.google.compute_route.assert_not_called()
                self.gateway.extract.assert_not_called()
        with self.assertRaises(GoogleError) as caught:
            self.prep.prepare({**self.form,'query':'Хочу кофе от условная 17б до условная 17б на час'})
        self.assertEqual(caught.exception.code,'route_endpoints_unresolved')
        self.google.resolve_place.assert_not_called()

    def test_numbered_street_endpoints_are_not_clock_times(self):
        for name in ('50 лет Казахстана 14','38-й улицы 12'):
            with self.subTest(name=name):
                final=self.prep.consume({**self.form,'query':'Хочу кофе от Mega до '+name+' на час'})
                self.assertEqual(final.resolved['resolved_context']['destination_source'],'google_places_text_search')
                self.assertEqual(self.google.resolve_place.call_args.args[0],name.casefold())
        from app.request_preparation import unique_place
        self.assertIsNone(unique_place([candidate('39-й улицы 12')],'38-й улицы 12'))
        self.assertIsNone(unique_place([candidate('38-й улицы 112')],'38-й улицы 12'))
        self.assertIsNone(unique_place([candidate('38-й проспекта 12')],'38-й улицы 12'))
        self.assertIsNone(unique_place([candidate('улица 7')],'7'))
        for query in ('Хочу гулять от Mega до 18:00',
                      'Хочу пешком гулять от Mega до двух часов'):
            self.assertIsNone(validate_walking_request(query,[120]))

    def test_explicit_extra_budget_adds_baseline_and_retains_reserve_inside_extra(self):
        for words,options in (('дополнительные 30 минут',[50]),('30 дополнительных минут',[50]),
                              ('лишние полчаса',[50]),('extra 30 minutes',[50]),
                              ('дополнительные 30–60 минут',[50,65,80])):
            query='Хочу кофе от EXPO до Mega, есть '+words
            with self.subTest(words=words):
                final=self.prep.consume({**self.form,'query':query})
                context=final.resolved['resolved_context']
                self.assertEqual(context['budget_mode'],'extra')
                self.assertEqual(context['budget_options'],options)
                self.assertEqual(context['extra_budget_options'],[value-20 for value in options])
                self.assertEqual(context['minutes'],options[0]);self.assertEqual(final.resolved['minutes'],options[0])
                self.assertEqual(final.resolved['route_baseline']['minutes'],20)
                self.assertEqual(final.form['query'],query)

    def test_ordinary_total_and_hour_before_flight_do_not_become_extra(self):
        for query in ('Хочу кофе от EXPO до Mega на час',
                      'У меня час до полёта, хочу кофе от EXPO до аэропорта'):
            with self.subTest(query=query):
                final=self.prep.consume({**self.form,'query':query})
                self.assertEqual(final.resolved['resolved_context']['budget_mode'],'total')
                self.assertEqual(final.resolved['resolved_context']['budget_options'],[60])

    def test_contradictory_total_extra_and_overflow_are_not_silently_capped(self):
        with self.assertRaises(GoogleError) as caught:
            self.prep.prepare({**self.form,'query':'Хочу кофе от EXPO до Mega на час и дополнительные 30 минут'})
        self.assertEqual(caught.exception.code,'route_budget_ambiguous')
        self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()
        with self.assertRaises(GoogleError) as caught:
            self.prep.prepare({**self.form,'query':'Хочу кофе от EXPO до Mega, дополнительные 480 минут'})
        self.assertEqual(caught.exception.code,'route_budget_out_of_range')
        self.assertEqual(self.google.compute_route.call_count,1);self.gateway.extract.assert_not_called()

    def test_gps_pending_resolves_destination_only_then_continues_once(self):
        form={**self.form,'query':'Хочу пешком дойти до условная 17б на час и кофе'}
        first=self.prep.prepare(form)
        self.assertTrue(first['needs_geolocation'])
        self.assertEqual(first['resolved_context']['preparation_phase'],'awaiting_origin')
        self.assertEqual(self.events,['place'])
        self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()
        resumed={**form,'preparation_token':first['preparation_token'],'location_status':'granted',
                 'geolocation':{'lat':51.12,'lon':71.43,'accuracy':20}}
        second=self.prep.prepare(resumed)
        self.assertFalse(second['needs_geolocation'])
        resumed['preparation_token']=second['preparation_token']
        final=self.prep.consume(resumed)
        self.assertEqual(self.events,['place','walk','intent'])
        self.assertEqual(final.resolved['resolved_context']['origin_source'],'browser_geolocation')
        self.assertEqual(final.resolved['origin']['lat'],51.12)
        self.assertEqual(final.place_attempts,1)
        with self.assertRaises(GoogleError):self.prep.consume(resumed)
        self.assertEqual(self.events,['place','walk','intent'])

    def test_gps_denied_outside_changed_query_and_expired_ticket_never_retry_destination(self):
        form={**self.form,'query':'Хочу пешком до условная 17б на час'}
        for change in ({'location_status':'denied'},
                       {'location_status':'granted','geolocation':{'lat':50.1,'lon':71.4}},
                       {'query':form['query']+'!'}):
            with self.subTest(change=change):
                first=self.prep.prepare(form);count=self.google.resolve_place.call_count
                with self.assertRaises((GoogleError,ContextError)):
                    self.prep.prepare({**form,'preparation_token':first['preparation_token'],**change})
                self.assertEqual(self.google.resolve_place.call_count,count)
        self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()

    def test_equal_coordinates_with_same_or_different_source_ids_never_call_baseline(self):
        for different_ids in (False,True):
            with self.subTest(different_ids=different_ids):
                self.google.resolve_place.side_effect=lambda name,**kw:[{**candidate('Дом один',('p1' if name.startswith('условная') else 'p2') if different_ids else 'p1'), 'address':'Астана, условная 17б, тестовая 28а'}]
                with self.assertRaises(GoogleError) as caught:self.prep.consume(self.form)
                self.assertEqual(caught.exception.code,'route_same_endpoint')
                self.google.compute_route.assert_not_called();self.gateway.extract.assert_not_called()

    def test_named_result_never_enters_token_and_changed_binding_is_detectable(self):
        first=self.prep.prepare(self.form)
        self.assertNotIn('условная',first['preparation_token'])
        final=self.prep.consume({**self.form,'preparation_token':first['preparation_token']})
        before=deepcopy(final.resolved['route_baseline'])
        changed=deepcopy(final.resolved['resolved_context']);changed['destination']['lon']+=0.01
        self.assertNotEqual(walking_bindings(changed)[1],before['destination_binding'])
        changed=deepcopy(final.resolved['resolved_context']);changed['destination_place_id']='another-id'
        self.assertNotEqual(walking_bindings(changed)[1],before['destination_binding'])

if __name__=='__main__':unittest.main()
