"""Offline tests for directed endpoints, independent budgets and stop claims."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from app.google_budget import GoogleError
from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.route_request import validate_walking_request, attach_walking_baseline
from app.unified_walks import _stop
from app.unified_narration import _plans
import test_unified_walks as fixtures
poi = fixtures.poi


class DirectedPlanningTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnifiedTests.setUp(self)
        self.addCleanup(self.map.close)
        self.query = 'Хочу прогуляться от Экспо до Mega на 45 минут'
        self.resolved = resolve_request({'query': self.query, 'time_of_day': 'day'})
        descriptor = validate_walking_request(self.query, [45])
        self.resolved = attach_walking_baseline(self.resolved, descriptor,
            {'routes': [{'duration': '600s', 'distanceMeters': 600, 'attribution': {'title': 'Google Maps', 'url': 'https://www.google.com/maps/'}}]})
        self.context = self.resolved['resolved_context']
        self.context['activity_mode'] = 'walk'
        self.memo = {('origin', 'destination'): deepcopy(self.resolved['route_baseline'])}
        self.stop = _stop(poi('osm:node/9', 'Синтетический парк', 51.09, 71.411), 'osm', 'o0')

    def plan(self, stops=None, budgets=None, **kwargs):
        return self.service._plans([self.stop] if stops is None else stops, budgets or [45], self.context,
                                  self.resolved['origin'], route_memo=self.memo, **kwargs)

    def test_directed_legs_map_and_no_repeated_baseline(self):
        result = self.plan()[0]
        self.assertTrue(result['route_built'])
        self.assertTrue(result['plan']['reaches_destination'])
        self.assertFalse(result['plan']['returns_to_origin'])
        self.assertFalse(result['plan']['direct_only'])
        self.assertEqual([(leg['from_ref'], leg['to_ref']) for leg in result['plan']['legs']],
                         [('origin', self.stop['stop_id']), (self.stop['stop_id'], 'destination')])
        self.assertEqual(len(self.google.legs), 2)
        self.assertEqual(self.google.legs[-1][1]['latLng'],
                         {'latitude': PUBLIC_ORIGINS['mega']['lat'], 'longitude': PUBLIC_ORIGINS['mega']['lon']})
        url = self.map.resolve({'ticket': result['map_ticket']})
        params = parse_qs(urlsplit(url['url']).query)
        self.assertNotEqual(params['origin'], params['destination'])
        self.assertIn('waypoints', params)

    def test_each_budget_rechecks_baseline_and_five_minute_reserve(self):
        options = self.plan([], [14, 15, 45])
        self.assertEqual([option['route_built'] for option in options], [False, True, True])
        self.assertEqual(options[1]['plan']['total_minutes'], 15)
        self.assertEqual(options[1]['plan']['visit_minutes'], 0)
        self.assertTrue(options[1]['plan']['direct_only'])
        self.assertIn('только', options[1]['route_note'])
        self.assertEqual(self.google.legs, [])
        self.assertIsNone(options[0]['map_ticket'])

    def test_direct_only_map_has_no_waypoint_and_never_narrates_venue(self):
        option = self.plan([])[0]
        params = parse_qs(urlsplit(self.map.resolve({'ticket': option['map_ticket']})['url']).query)
        self.assertNotIn('waypoints', params)
        _, any_fit, all_fit = _plans([option], [], self.context)
        self.assertFalse(any_fit)
        self.assertFalse(all_fit)

    def test_minimum_stop_needs_own_five_minutes_after_full_trip(self):
        self.google.compute_route = Mock(return_value={'routes': [{'duration': '420s', 'distanceMeters': 600, 'attribution': {'title': 'Google Maps', 'url': 'https://www.google.com/maps/'}}]})
        options = self.plan(budgets=[20, 24])
        self.assertTrue(options[0]['plan']['direct_only'])
        self.assertFalse(options[1]['plan']['direct_only'])
        self.assertEqual(options[1]['plan']['visit_minutes'], 5)
        self.assertEqual(options[1]['plan']['total_minutes'], 24)

    def test_failed_final_leg_keeps_only_checked_direct_transfer(self):
        self.google.compute_route = Mock(side_effect=[{'routes': [{'duration': '120s', 'distanceMeters': 600, 'attribution': {'title': 'Google Maps', 'url': 'https://www.google.com/maps/'}}]},
             GoogleError('google_timeout', 'Synthetic', 504)])
        option = self.plan()[0]
        self.assertTrue(option['plan']['direct_only'])
        self.assertEqual(option['plan']['legs'], [self.resolved['route_baseline']])
        self.assertFalse(option['plan']['route_checks'][0]['final_verified'])

    def test_schedule_covers_whole_visit_and_final_leg_is_in_total(self):
        self.stop['opening_hours'] = 'We 10:00-10:10'
        option = self.plan(schedule_when=datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc))[0]
        self.assertTrue(option['plan']['direct_only'])
        self.assertEqual(option['plan']['schedule_reason'], 'reported_schedule_does_not_cover_interval')

    def test_narration_checks_endpoint_chain_and_separate_roundtrip_guard(self):
        option = self.plan()[0]
        self.assertTrue(_plans([option], [self.stop['stop_id']], self.context)[1])
        for mutate in ('destination', 'chain', 'return', 'total', 'direct'):
            broken = deepcopy(option)
            if mutate == 'destination':
                broken['plan']['destination'] = PUBLIC_ORIGINS['airport']
            elif mutate == 'chain':
                broken['plan']['legs'][-1]['to_ref'] = 'origin'
            elif mutate == 'return':
                broken['plan']['returns_to_origin'] = True
            elif mutate == 'total':
                broken['plan']['total_minutes'] -= 1
            else:
                broken['plan']['direct_only'] = True
            with self.subTest(mutate=mutate):
                self.assertFalse(_plans([broken], [self.stop['stop_id']], self.context)[1])
        self.assertFalse(_plans([option], [self.stop['stop_id']], {'route_kind': 'round_trip'})[1])

    def test_missing_or_swapped_baseline_fails_before_planning_io(self):
        for baseline in (None, {**self.resolved['route_baseline'], 'to_ref': 'origin'}):
            with self.subTest(baseline=baseline), self.assertRaises(GoogleError):
                self.service._plans([self.stop], [45], self.context, self.resolved['origin'],
                                    route_memo={('origin', 'destination'): baseline})
        self.assertEqual(self.google.legs, [])

    def test_legacy_service_checks_baseline_once_then_sources_and_stop_legs(self):
        result = self.service.recommend({'query': self.query, 'time_of_day': 'day', 'consent': True})
        self.assertEqual(result['resolved_context']['route_kind'], 'point_to_point')
        baseline = ({'latLng': {'latitude': PUBLIC_ORIGINS['expo']['lat'], 'longitude': PUBLIC_ORIGINS['expo']['lon']}},
                    {'latLng': {'latitude': PUBLIC_ORIGINS['mega']['lat'], 'longitude': PUBLIC_ORIGINS['mega']['lon']}})
        self.assertEqual(self.google.legs.count(baseline), 1)
        self.assertEqual(self.google.search_count, 1)
        self.assertEqual(self.dgis.search.call_count, 1)
        self.assertEqual(self.narrator.call_count, 1)
        self.assertTrue(all(option['plan']['reaches_destination'] for option in result['alternatives']))

    def test_legacy_long_baseline_stops_all_source_and_narration_calls(self):
        self.google.compute_route = Mock(return_value={'routes': [{'duration': '3600s', 'distanceMeters': 3600, 'attribution': {'title': 'Google Maps', 'url': 'https://www.google.com/maps/'}}]})
        with self.assertRaises(GoogleError) as error:
            self.service.recommend({'query': self.query, 'time_of_day': 'day', 'consent': True})
        self.assertEqual(error.exception.code, 'route_does_not_fit')
        self.assertEqual(self.google.search_count, 0)
        self.osm.retrieve.assert_not_called()
        self.dgis.search.assert_not_called()
        self.narrator.assert_not_called()

    def test_direct_transfer_does_not_block_unknown_socket_alternative(self):
        cafe = poi('osm:node/8', 'Кафе из синтетического источника', 51.09, 71.411, 'cafe')
        self.osm.retrieve.return_value = {'selected': [{'poi': cafe}]}
        self.dgis.search.return_value = {'places': [], 'warnings': []}
        result = self.service.recommend({'query': 'Хочу кафе с розеткой от Экспо до Mega на 45 минут',
                                         'time_of_day': 'day', 'consent': True})
        option = result['alternatives'][0]
        self.assertTrue(option['route_built'])
        self.assertFalse(option['plan']['direct_only'])
        self.assertEqual(option['amenity_fallback']['unconfirmed'], ['socket'])
        self.assertEqual(option['stops'][0]['category'], 'cafe')
        self.assertEqual(option['stops'][0]['features'].get('socket', 'unknown'), 'unknown')


if __name__ == '__main__':
    unittest.main()
