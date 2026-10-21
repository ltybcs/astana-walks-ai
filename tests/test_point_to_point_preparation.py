"""Public direct walking preparation with in-memory provider/model stubs only."""
from contextlib import nullcontext
from copy import deepcopy
import unittest
from unittest.mock import Mock

from app.google_budget import GoogleError
from app.lab_context import ContextError, PUBLIC_ORIGINS, resolve_request, stage_request
from app.request_preparation import RequestPreparation, prepare_walking_baseline
from app.route_request import (attach_walking_baseline, validate_walking_request,
                               walking_waypoints)
from app.semantic_intent import deterministic_plan


def route(minutes=12):
    return {'routes':[{'duration':str(minutes*60)+'s', 'distanceMeters':800,
             'attribution':{'title':'Google Maps','url':'https://maps.google.com/'}}]}


class PublicWalkingPreparationTests(unittest.TestCase):
    def setUp(self):
        self.now=100.0
        self.query='Хочу кофе от EXPO до Mega на час'
        self.form={'query':self.query,'consent':True,'time_of_day':'day'}
        self.events=[]
        self.google=Mock()
        self.google.workflow.side_effect=lambda seconds:nullcontext()
        self.google.compute_route.side_effect=lambda a,b:(self.events.append('walk') or route())
        self.gateway=Mock()
        self.gateway.extract.side_effect=lambda query,deadline:(self.events.append('intent') or
            (deterministic_plan(query),'semantic',1))
        self.prep=RequestPreparation(resolve_request,lambda form,**kw:stage_request(form,probe=True,**kw),
            self.google,self.gateway,clock=lambda:self.now)

    def test_one_direct_leg_precedes_model_and_survives_one_use_token(self):
        prepared=self.prep.prepare(self.form)
        self.assertEqual(self.events,['walk','intent'])
        final=self.prep.consume({**self.form,'preparation_token':prepared['preparation_token']})
        context=final.resolved['resolved_context']
        self.assertEqual(context['route_kind'],'point_to_point')
        self.assertEqual(context['origin_id'],'expo')
        self.assertEqual(context['destination_id'],'mega')
        self.assertEqual(context['destination'],PUBLIC_ORIGINS['mega'])
        self.assertEqual(context['direct_walk_minutes'],12)
        self.assertEqual(set(final.resolved['route_baseline']),
                         {'from_ref','to_ref','minutes','attribution','error_code','origin_id','destination_id','origin_binding','destination_binding'})
        self.assertEqual(final.resolved['route_baseline']['to_ref'],'destination')
        self.assertEqual(final.resolved['route_baseline']['origin_id'],'expo')
        self.assertEqual(final.resolved['route_baseline']['destination_id'],'mega')
        self.assertEqual(final.intent_plan.search_intent['categories'],['cafe'])
        self.assertEqual(self.google.compute_route.call_count,1)
        self.google.resolve_place.assert_not_called()
        with self.assertRaises(GoogleError):
            self.prep.consume({**self.form,'preparation_token':prepared['preparation_token']})
        self.assertEqual(self.google.compute_route.call_count,1)

    def test_pair_origin_overrides_earlier_landmark_gps_and_model_origin(self):
        self.form.update(query='Я возле Байтерека. Хочу кофе от EXPO до Mega на час',
                         geolocation={'lat':51.15,'lon':71.45},location_status='granted')
        plan=deterministic_plan(self.form['query'])
        from dataclasses import replace
        self.gateway.extract.side_effect=lambda *args:(replace(plan,start_quote='Байтерека',extraction_mode='semantic'),'semantic',1)
        final=self.prep.consume(self.form)
        self.assertEqual(final.resolved['origin'],PUBLIC_ORIGINS['expo'])
        self.assertFalse(final.resolved['needs_geolocation'])
        self.assertFalse(final.resolved['resolved_context']['sensitive_origin'])
        self.google.resolve_place.assert_not_called()

    def test_actual_direct_duration_and_five_minute_reserve_gate_before_model(self):
        for minutes in (56,61):
            with self.subTest(minutes=minutes):
                self.google.compute_route.side_effect=lambda a,b:route(minutes)
                with self.assertRaises(GoogleError) as caught:self.prep.prepare(self.form)
                self.assertEqual((caught.exception.code,caught.exception.status),('route_does_not_fit',422))
                self.assertIn(str(minutes)+' мин',caught.exception.message)
                self.assertIn('запасом 5 мин',caught.exception.message)
                self.gateway.extract.assert_not_called()
        self.assertEqual(self.google.compute_route.call_count,2)

    def test_invalid_or_unavailable_direct_route_never_runs_intent(self):
        for response in ({},{'routes':[]},route(0),{'routes':[{'duration':'nan','distanceMeters':1}]}):
            with self.subTest(response=response):
                self.google.compute_route.side_effect=lambda a,b:response
                with self.assertRaises(GoogleError) as caught:self.prep.prepare(self.form)
                self.assertEqual(caught.exception.code,'direct_route_unverified')
                self.gateway.extract.assert_not_called()
        for error in (GoogleError('google_rate_limited','PRIVATE',429),OSError('PRIVATE')):
            with self.subTest(kind=type(error).__name__):
                self.google.compute_route.side_effect=error
                with self.assertRaises(GoogleError) as caught:self.prep.prepare(self.form)
                self.assertIn(caught.exception.code,{'direct_route_unavailable','direct_route_timeout'})
                self.assertNotIn('PRIVATE',caught.exception.message)
                self.gateway.extract.assert_not_called()

    def test_bad_consent_fields_clock_or_gps_are_rejected_before_baseline(self):
        for change in ({'consent':False},{'minutes':True},{'unexpected':True},
                       {'geolocation':{'lat':float('nan'),'lon':71.4}},
                       {'client_context':{'instant':'2000-01-01T00:00:00Z','utc_offset_minutes':300}}):
            with self.subTest(change=change):
                with self.assertRaises((GoogleError,ContextError)):self.prep.prepare({**self.form,**change})
                self.google.compute_route.assert_not_called()
                self.gateway.extract.assert_not_called()

    def test_unknown_partial_ambiguous_and_nonwalking_endpoints_never_use_providers(self):
        cases=[('Хочу кофе от EXPO до неизвестного отеля','route_endpoints_unresolved'),
               ('Хочу кофе от неизвестного отеля до Mega','route_endpoints_unresolved'),
               ('Хочу кофе от EXPO до','route_endpoints_unresolved'),
               ('Хочу кофе от EXPO до Mega или Байтерека','unsupported_point_to_point'),
               ('Хочу на машине от EXPO до Mega','unsupported_route_mode')]
        for query,code in cases:
            with self.subTest(query=query):
                with self.assertRaises(GoogleError) as caught:self.prep.prepare({**self.form,'query':query})
                self.assertEqual(caught.exception.code,code)
                self.google.compute_route.assert_not_called()
                self.google.resolve_place.assert_not_called()
                self.gateway.extract.assert_not_called()

    def test_changed_form_token_does_not_repeat_baseline(self):
        response=self.prep.prepare(self.form)
        with self.assertRaises(GoogleError) as caught:
            self.prep.consume({**self.form,'query':self.query+'!',
                              'preparation_token':response['preparation_token']})
        self.assertEqual(caught.exception.code,'invalid_preparation')
        self.assertEqual(self.google.compute_route.call_count,1)

    def test_same_anchor_and_negated_route_keep_existing_roundtrip(self):
        for query in ('Хочу кофе от Mega до Меги на час',
                      'Не хочу от EXPO до аэропорта, хочу кофе возле Mega'):
            with self.subTest(query=query):
                final=self.prep.consume({**self.form,'query':query})
                self.assertNotIn('route_baseline',final.resolved)
                self.google.compute_route.assert_not_called()

    def test_pure_attachment_identity_nonmutation_and_largest_option(self):
        descriptor=validate_walking_request('От EXPO до Mega',[30,60])
        resolved=stage_request({'query':'Хочу кофе возле Mega','minutes':30},probe=True)
        resolved['resolved_context']['budget_options']=[30,60]
        original=deepcopy((descriptor,resolved))
        response=route(35);body=deepcopy(response)
        result=attach_walking_baseline(resolved,descriptor,response)
        self.assertEqual((descriptor,resolved),original)
        self.assertEqual(response,body)
        self.assertEqual(result['resolved_context']['budget_options'],[30,60])
        result['route_baseline']['attribution']['title']='CHANGED'
        self.assertEqual(response,body)
        for key,new_id in (('origin_id','baiterek'),('destination_id','airport')):
            poisoned=deepcopy(descriptor);poisoned[key]=new_id
            with self.assertRaises(ValueError):attach_walking_baseline(resolved,poisoned,response)
        descriptor['origin']['lat']+=0.01
        with self.assertRaises(ValueError):walking_waypoints(descriptor)

    def test_all_public_pairs_use_exact_catalogue_coordinates(self):
        for identifier in PUBLIC_ORIGINS:
            other='mega' if identifier!='mega' else 'expo'
            from app.lab_context import ORIGIN_REFERENCES
            desc={'route_kind':'point_to_point','origin_id':identifier,
                  'destination_id':other,'origin':deepcopy(PUBLIC_ORIGINS[identifier]),
                  'destination':deepcopy(PUBLIC_ORIGINS[other])}
            first,last=walking_waypoints(desc)
            self.assertEqual(first,{'latLng':{'latitude':PUBLIC_ORIGINS[identifier]['lat'],
                                            'longitude':PUBLIC_ORIGINS[identifier]['lon']}})
            self.assertEqual(last['latLng']['latitude'],PUBLIC_ORIGINS[other]['lat'])


    def test_generic_sightseeing_fallback_keeps_literal_goal_and_never_erases_service(self):
        self.gateway.extract.side_effect=lambda *args:(None,'invalid_intent',1)
        query='Покажи что можно по пути посмотреть от EXPO до Mega за 60 минут'
        final=self.prep.consume({**self.form,'query':query})
        self.assertEqual(final.intent_plan.goals[0].quote,'что можно по пути посмотреть')
        self.assertEqual(final.intent_plan.search_intent['categories'],['walk'])
        for extra in (' и погладить капибару',' только с розетками',' и хочу планетарий'):
            with self.subTest(extra=extra),self.assertRaises(GoogleError) as caught:
                self.prep.prepare({**self.form,'query':query+extra})
            self.assertEqual(caught.exception.code,'intent_not_understood')
        final=self.prep.consume({**self.form,'query':'Хочу кафе с розетками от EXPO до Mega на час'})
        self.assertEqual(final.intent_plan.search_intent['categories'],['cafe'])
        self.assertIn('socket',final.intent_plan.required_keys)

    def test_public_departure_prepositions_do_not_turn_destination_into_start(self):
        for query in ('Хочу кофе с EXPO до Mega на час','Хочу кофе из EXPO в Mega на час'):
            with self.subTest(query=query):
                final=self.prep.consume({**self.form,'query':query})
                self.assertEqual(final.resolved['resolved_context']['origin_id'],'expo')
                self.assertEqual(final.resolved['resolved_context']['destination_id'],'mega')

    def test_absolute_deadline_before_and_after_single_baseline(self):
        descriptor=validate_walking_request(self.query,[60])
        resolved=stage_request({'query':self.query},probe=True)
        with self.assertRaises(GoogleError) as caught:
            prepare_walking_baseline(resolved,descriptor,self.google,deadline=100,clock=lambda:100)
        self.assertEqual(caught.exception.code,'direct_route_timeout')
        self.google.compute_route.assert_not_called()
        clock=iter([100,102])
        with self.assertRaises(GoogleError) as caught:
            prepare_walking_baseline(resolved,descriptor,self.google,deadline=101,clock=lambda:next(clock))
        self.assertEqual(caught.exception.code,'direct_route_timeout')
        self.assertEqual(self.google.compute_route.call_count,1)


if __name__=='__main__':unittest.main()
