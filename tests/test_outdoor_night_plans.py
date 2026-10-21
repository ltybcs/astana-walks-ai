"""Conditional outdoor night plans with real retrieval and fake live providers."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
from time import monotonic

from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.retrieval import Retriever
from app.request_preparation import PreparedRequest
from app.semantic_intent import IntentPlan, Goal
from app.route_map import RouteMap
from app.unified_walks import UnifiedWalks
from tests.test_unified_walks import Google, poi


class OutdoorNightPlansTests(unittest.TestCase):
    def build(self, *, places=None, live=None, rejected=False):
        origin = PUBLIC_ORIGINS['expo']
        green = poi('osm:node/1', 'Синтетический парк', origin['lat']+.003, origin['lon'], 'park')
        dgis = Mock()
        dgis.status.return_value = {'available': True, 'configured': True}
        dgis.search.return_value = {'places': deepcopy(live or []), 'warnings': []}
        retriever = Retriever(data={'dataset': {'is_demo': True}, 'places': deepcopy(places if places is not None else [green])})
        google = Google()
        def narrate(_grounding, stops, *_):
            if rejected:
                return None, 'unsupported_quality_claim'
            return {'text': 'Синтетическое проверенное описание.',
                    'source_ids': [s['source_id'] for s in stops],
                    'stop_ids': [s['stop_id'] for s in stops],
                    'venue_descriptions': [{'stop_id': s['stop_id'], 'text': 'Прогулка после проверки условий.'} for s in stops]}, 'mock_validated'
        narrator = Mock(side_effect=narrate)
        route_map = RouteMap('PUBLIC_TEST_EMBED_KEY')
        self.addCleanup(route_map.close)
        service = UnifiedWalks(resolve_request, retriever, google, dgis, narrator=narrator, route_map=route_map)
        return service, google, narrator

    def form(self, **changes):
        return {'query': 'У меня 1,5 часа, я около экспо, хочу увидеть природу Астаны',
                'time_of_day': 'night', 'consent': True, **changes}

    def test_exact_nature_request_reaches_verified_route_with_visible_conditional_note(self):
        for rejected in (False, True):
            with self.subTest(model_rejected=rejected):
                service, google, narrator = self.build(rejected=rejected)
                result = service.recommend(self.form())
                option = result['alternatives'][0]
                self.assertEqual(option['budget_minutes'], 90)
                self.assertTrue(option['route_built'])
                self.assertTrue(option['plan']['returns_to_origin'])
                self.assertLessEqual(option['plan']['total_minutes'], 90)
                stop = option['stops'][0]
                self.assertEqual(stop['category'], 'park')
                self.assertIsNone(stop['opening_hours'])
                self.assertIn('Ночной доступ и освещение не подтверждены', stop['availability_note'])
                self.assertEqual(stop['description_origin'], 'source-template' if rejected else 'local-model-with-note')
                self.assertNotIn('круглосуточ', stop['user_description'])
                self.assertTrue(stop['user_description'].endswith(' Примечание приложения: '+stop['availability_note']))
                self.assertLessEqual(len(stop['user_description']), 600)
                if not rejected:
                    self.assertNotIn('Примечание приложения', result['answer']['venue_descriptions'][0]['text'])
                self.assertEqual(result['source_receipt']['osm']['selected_count'], 1)
                self.assertEqual(result['source_receipt']['google']['candidate_count'], 0)
                self.assertEqual(google.search_count, 1)
                self.assertEqual(len(google.legs), 2)
                narrator.assert_called_once()
                self.assertIn('Полный синтетический ответ Google', result['grounding']['summary'])

    def test_adapter_places_are_rechecked_for_access_known_hours_and_venue_type(self):
        origin = PUBLIC_ORIGINS['expo']
        base = poi('2gis:green', 'Синтетический сквер', origin['lat']+.002, origin['lon'], 'park')
        private = {**base, 'id': '2gis:private', 'source_tags': {'access': 'private'}}
        closed = {**base, 'id': '2gis:closed', 'opening_hours': 'Mo-Su 09:00-18:00'}
        cafe = {**base, 'id': '2gis:cafe', 'category': 'cafe'}
        service, _, _ = self.build(places=[], live=[private, closed, cafe, base])
        result = service.recommend(self.form())
        self.assertEqual(result['source_receipt']['dgis']['candidate_count'], 1)
        self.assertEqual([s['id'] for s in result['alternatives'][0]['stops']], ['2gis:green'])
        self.assertIn('не подтверждены', result['alternatives'][0]['stops'][0]['availability_note'])

    def test_unknown_indoor_night_hours_stay_hard_but_outdoor_socket_is_labelled(self):
        origin = PUBLIC_ORIGINS['expo']
        cafe = poi('osm:node/2', 'Синтетическая кофейня', origin['lat']+.002, origin['lon'], 'cafe')
        for query, places in [('Хочу кофе около Экспо на час', [cafe]),
                              (self.form()['query']+'; обязательно нужна розетка', None)]:
            with self.subTest(query=query):
                service, google, narrator = self.build(places=places)
                result = service.recommend(self.form(query=query))
                if places is not None:
                    self.assertFalse(any(o['route_built'] for o in result['alternatives']))
                    self.assertEqual(google.legs, [])
                    narrator.assert_not_called()
                else:
                    option = result['alternatives'][0]
                    self.assertTrue(option['route_built'])
                    self.assertEqual(option['amenity_fallback']['unconfirmed'], ['socket'])
                    for stop in option['stops']:
                        self.assertNotEqual(stop['features'].get('socket'), 'yes')
                        self.assertIn('не подтверждены', stop['availability_note'])
                        self.assertIn('Не подтверждены', stop['amenity_note'])

    def test_explicit_24x7_requirement_stays_hard_in_day_and_night(self):
        for period in ('day', 'night'):
            with self.subTest(period=period):
                service, google, narrator = self.build()
                payload = self.form(query='Хочу только круглосуточный парк около Экспо на час', time_of_day=period)
                form = {k: v for k, v in payload.items() if k != 'consent'}
                plan = IntentPlan('Экспо', (Goal('парк', 'Парки и сады', 'park', 'venue_visit'),), (), 'walk_mix', (), ())
                service.preparation = Mock()
                service.preparation.consume.return_value = PreparedRequest(form, resolve_request(form), plan,
                    monotonic()+200, 'synthetic_semantic', 0, 0)
                result = service.recommend(payload)
                self.assertFalse(any(o['route_built'] for o in result['alternatives']))
                self.assertEqual(google.legs, [])
                narrator.assert_not_called()

    def test_explicit_opening_demand_cannot_be_bypassed_by_untyped_google_walk(self):
        service, google, narrator = self.build(places=[])
        payload = self.form(query='Хочу прогулку около Экспо; открыто сейчас', time_of_day='day')
        form = {k: v for k, v in payload.items() if k != 'consent'}
        plan = IntentPlan('Экспо', (Goal('прогулку', 'Прогулка', 'walk', 'generic_walk'),), (), 'walk_mix', (), ())
        service.preparation = Mock()
        service.preparation.consume.return_value = PreparedRequest(form, resolve_request(form), plan,
            monotonic()+200, 'synthetic_semantic', 0, 0)
        result = service.recommend(payload)
        self.assertEqual(result['source_receipt']['google']['candidate_count'], 0)
        self.assertFalse(any(o['route_built'] for o in result['alternatives']))
        self.assertEqual(google.legs, [])
        narrator.assert_not_called()

    def test_day_and_own_24x7_do_not_receive_unknown_night_notice(self):
        origin = PUBLIC_ORIGINS['expo']
        always = {**poi('osm:node/3', 'Синтетический парк', origin['lat']+.002, origin['lon']), 'opening_hours': '24/7'}
        for period, places in [('day', None), ('night', [always])]:
            with self.subTest(period=period):
                service, _, _ = self.build(places=places)
                stop = service.recommend(self.form(time_of_day=period))['alternatives'][0]['stops'][0]
                self.assertNotIn('availability_note', stop)


if __name__ == '__main__':
    unittest.main()
