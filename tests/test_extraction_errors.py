"""Extraction failures retain bounded reasons; no real model or provider I/O."""
from contextlib import contextmanager
import io
import json
from time import monotonic
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from app.google_budget import GoogleError
from app.lab_context import resolve_request, stage_request
from app.llm import LLMConfig
from app.model_gateway import ModelGateway, ModelUnavailable
from app.request_preparation import RequestPreparation


def valid_intent():
    return {'start_quote': None, 'goals': [{'quote': 'кофе', 'search_text': 'кафе',
            'category_hint': 'cafe', 'fulfilment': 'venue_visit'}],
            'goal_policy': 'primary', 'excluded': [], 'requirements': [], 'context': []}


def body(content, finish='stop'):
    return json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': finish}]},
                      ensure_ascii=False).encode()


class ExtractionErrorsTests(unittest.TestCase):
    def gateway(self, raw):
        opener = Mock(side_effect=lambda *_args, **_kw: io.BytesIO(raw))
        gateway = ModelGateway(LLMConfig('http://127.0.0.1:8081/v1', 'mock', '', 2), http_open=opener)
        return gateway, opener

    def test_valid_content_is_validated_once_without_retry(self):
        gateway, opener = self.gateway(body(json.dumps(valid_intent(), ensure_ascii=False)))
        plan, status, attempts = gateway.extract('Хочу кофе', monotonic()+5)
        self.assertIsNotNone(plan)
        self.assertEqual((status, attempts), ('semantic', 1))
        opener.assert_called_once()

    def test_provider_content_shape_truncation_and_semantic_failures_are_not_outages(self):
        invalid = valid_intent()
        invalid['goals'][0]['quote'] = 'PRIVATE_NOT_IN_QUERY'
        cases = [(b'{PRIVATE_MODEL_BODY', 'provider_json'), (b'{}', 'invalid_response'),
                 (body('{PRIVATE_MODEL_BODY'), 'content_json'),
                 (body(json.dumps(valid_intent()), 'length'), 'truncated_response'),
                 (body(json.dumps(valid_intent()), 'content_filter'), 'invalid_response'),
                 (body(json.dumps(invalid)), 'invalid_intent'),
                 (b'x'*100001, 'invalid_response')]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                gateway, opener = self.gateway(raw)
                result = gateway.extract('Хочу кофе', monotonic()+5)
                self.assertEqual(result, (None, expected, 1))
                self.assertNotIn('PRIVATE', repr(result))
                opener.assert_called_once()

    def test_input_schema_rejection_is_not_transport_unavailability(self):
        gateway, opener = self.gateway(b'{}')
        with patch('app.model_gateway._PROCESS_LOCK') as lock:
            self.assertEqual(gateway.extract('Хочу кофе https://example.test', monotonic()+5),
                             (None, 'invalid_intent', 0))
            lock.acquire.assert_not_called()
        opener.assert_not_called()

    def test_busy_and_elapsed_deadline_are_distinct_zero_attempt_failures(self):
        gateway, opener = self.gateway(b'{}')
        with patch('app.model_gateway._PROCESS_LOCK') as lock:
            lock.acquire.return_value = False
            self.assertEqual(gateway.extract('Хочу кофе', monotonic()+5), (None, 'busy', 0))
            lock.release.assert_not_called()
        self.assertEqual(gateway.extract('Хочу кофе', monotonic()-1), (None, 'deadline', 0))
        opener.assert_not_called()

    def test_transport_and_read_timeout_preserve_one_attempt_without_private_errors(self):
        for error, expected in [(URLError('PRIVATE_NETWORK_REASON'), 'transport'),
                                (URLError(TimeoutError('PRIVATE_TIMEOUT')), 'deadline'),
                                (TimeoutError('PRIVATE_TIMEOUT'), 'deadline')]:
            with self.subTest(expected=expected):
                opener = Mock(side_effect=error)
                gateway = ModelGateway(LLMConfig('http://127.0.0.1:8081/v1', 'mock', '', 2), http_open=opener)
                result = gateway.extract('Хочу кофе', monotonic()+5)
                self.assertEqual(result, (None, expected, 1))
                self.assertNotIn('PRIVATE', repr(result))
                opener.assert_called_once()

    def test_http_format_rejection_is_not_transport_and_never_reads_private_body(self):
        for status, expected in ((400,'invalid_response'),(422,'invalid_response'),(429,'busy'),
                (503,'busy'),(408,'deadline'),(504,'deadline'),(401,'unconfigured'),(500,'transport')):
            with self.subTest(status=status):
                response = io.BytesIO(b'PRIVATE_PROVIDER_ERROR')
                error = HTTPError('http://127.0.0.1:8081/v1/chat/completions', status,
                                  'PRIVATE_PROVIDER_REASON', {}, response)
                opener=Mock(side_effect=error)
                gateway=ModelGateway(LLMConfig('http://127.0.0.1:8081/v1', 'mock', '', 2),http_open=opener)
                result=gateway.extract('Хочу кофе',monotonic()+5)
                self.assertEqual(result,(None,expected,1))
                self.assertNotIn('PRIVATE',repr(result))
                self.assertTrue(response.closed)
                opener.assert_called_once()

    def test_shared_os_lock_timeout_is_busy_without_model_http(self):
        gateway, opener = self.gateway(b'{}')
        gateway.lock_path = object()
        @contextmanager
        def locked(*_args, **_kw):
            raise GoogleError('quota_unavailable', 'PRIVATE_LOCK_DETAIL', 503)
            yield
        with patch('app.model_gateway._locked', locked):
            self.assertEqual(gateway.extract('Хочу кофе', monotonic()+5), (None, 'busy', 0))
        opener.assert_not_called()


class PreparationFailureTests(unittest.TestCase):
    def build(self, status):
        gateway, google = Mock(), Mock()
        gateway.extract.return_value = (None, status, 0 if status in {'busy', 'unconfigured'} else 1)
        prep = RequestPreparation(resolve_request, lambda form, **kw: stage_request(form, probe=True, **kw),
                                  google, gateway)
        return prep, gateway, google

    def test_known_relative_park_query_reaches_gps_after_any_model_failure(self):
        for status in ('busy', 'transport', 'deadline', 'provider_json', 'content_json', 'invalid_intent', 'truncated_response'):
            with self.subTest(status=status):
                prep, gateway, google = self.build(status)
                form = {'query': 'Дай парки в которые можно сходить ближайшие 2 часа рядом со мной',
                        'time_of_day': 'night', 'consent': True}
                first = prep.prepare(form)
                self.assertTrue(first['needs_geolocation'])
                self.assertEqual(first['resolved_context']['minutes'], 120)
                self.assertIsNone(first['resolved_context']['origin'])
                # This is a public synthetic stand-in, not device GPS.
                resumed = {**form, 'preparation_token': first['preparation_token'], 'location_status': 'granted',
                           'geolocation': {'lat': 51.09125, 'lon': 71.412806, 'accuracy': 20}}
                second = prep.prepare(resumed)
                resumed['preparation_token'] = second['preparation_token']
                final = prep.consume(resumed)
                self.assertEqual(final.intent_plan.search_intent['categories'], ['park'])
                self.assertEqual(final.resolved['origin']['lat'], 51.09125)
                self.assertEqual(final.resolved['minutes'], 120)
                gateway.extract.assert_called_once()
                google.resolve_place.assert_not_called()

    def test_generic_arrival_query_requests_gps_without_inventing_airport_or_flags(self):
        for status in ('busy', 'transport', 'deadline', 'provider_json', 'content_json', 'invalid_intent', 'truncated_response'):
            with self.subTest(status=status):
                prep, gateway, google = self.build(status)
                form = {'query': 'Я только что прилетел, что можно посмотреть в Астане', 'consent': True}
                first = prep.prepare(form)
                self.assertTrue(first['needs_geolocation'])
                self.assertIsNone(first['resolved_context']['origin'])
                self.assertEqual(first['resolved_context']['budget_options'], [60,120,240])
                resumed = {**form, 'preparation_token':first['preparation_token'], 'location_status':'granted',
                           'geolocation':{'lat':51.09125,'lon':71.412806,'accuracy':20}}
                second = prep.prepare(resumed)
                resumed['preparation_token'] = second['preparation_token']
                final = prep.consume(resumed)
                self.assertEqual(final.intent_plan.mode, 'walk')
                self.assertEqual(final.intent_plan.context, ())
                self.assertEqual(final.resolved['resolved_context']['origin_source'], 'browser_geolocation')
                self.assertIsNone(final.resolved['resolved_context']['origin_id'])
                gateway.extract.assert_called_once()
                google.resolve_place.assert_not_called()

    @staticmethod
    def numbered_place(identifier='synthetic-1'):
        # Synthetic resolver data at a fixed public stand-in point, not an actual building record.
        return {'id':identifier, 'name':'Камал-5', 'address':'Астана', 'lat':51.09125, 'lon':71.412806,
                'source_url':'https://www.google.com/maps/search/?api=1&query=synthetic', 'attributions':[]}

    def test_named_departure_and_whole_day_resolve_once_without_gps_or_reextraction(self):
        for status in ('busy', 'transport', 'deadline', 'provider_json', 'invalid_intent', 'truncated_response'):
            with self.subTest(status=status):
                prep, gateway, google = self.build(status)
                google.resolve_place.return_value = [self.numbered_place()]
                form = {'query':'Мне скучно, у меня есть целый день. Куда можно пойти с Камал-5', 'consent':True}
                response = prep.prepare(form)
                self.assertFalse(response['needs_geolocation'])
                self.assertEqual(response['resolved_context']['budget_options'], [240,360,480])
                self.assertEqual(response['resolved_context']['origin_source'], 'google_places_text_search')
                final = prep.consume({**form, 'preparation_token':response['preparation_token']})
                self.assertEqual(final.place_attempts, 1)
                self.assertEqual(final.intent_plan.mode, 'walk')
                gateway.extract.assert_called_once()
                google.resolve_place.assert_called_once()
                self.assertEqual(google.resolve_place.call_args.args[0].casefold(), 'камал-5')

    def test_ambiguous_named_departure_does_not_switch_to_available_gps(self):
        for candidates in ([], [self.numbered_place(), self.numbered_place('synthetic-2')]):
            prep, gateway, google = self.build('invalid_intent')
            google.resolve_place.return_value = candidates
            with self.assertRaises(Exception) as caught:
                prep.prepare({'query':'Мне скучно, у меня есть целый день. Куда можно пойти с Камал-5',
                    'consent':True, 'location_status':'granted',
                    'geolocation':{'lat':51.09125,'lon':71.412806,'accuracy':20}})
            self.assertEqual((caught.exception.code,caught.exception.status), ('location_unresolved',422))
            gateway.extract.assert_called_once()
            google.resolve_place.assert_called_once()

    def test_unknown_goal_error_distinguishes_interpretation_outage_and_deadline(self):
        statuses = {'busy': (503, 'intent_temporarily_unavailable'),
                    'transport': (503, 'intent_temporarily_unavailable'),
                    'unconfigured': (503, 'intent_temporarily_unavailable'),
                    'deadline': (504, 'intent_timeout'),
                    'provider_json': (422, 'intent_not_understood'),
                    'content_json': (422, 'intent_not_understood'),
                    'invalid_intent': (422, 'intent_not_understood'),
                    'invalid_response': (422, 'intent_not_understood'),
                    'truncated_response': (422, 'intent_not_understood')}
        for reason, expected in statuses.items():
            with self.subTest(reason=reason):
                prep, gateway, google = self.build(reason)
                with self.assertRaises(GoogleError) as caught:
                    prep.prepare({'query': 'Хочу погладить капибару возле Байтерека', 'consent': True})
                self.assertEqual((caught.exception.status, caught.exception.code), expected)
                self.assertNotIn('капибар', caught.exception.message)
                gateway.extract.assert_called_once()
                google.resolve_place.assert_not_called()

    def test_point_to_point_requests_stop_before_model_or_places(self):
        cases = [
            ('У меня 10 минут до полёта, покажи что можно по пути посмотреть от экспо до аэропорта', 'route_does_not_fit'),
            ('Хочу прогулку от неизвестного отеля до Абу Даби на 2 часа', 'route_endpoints_unresolved')]
        for query, code in cases:
            with self.subTest(code=code):
                prep, gateway, google = self.build('invalid_intent')
                with self.assertRaises(GoogleError) as caught:
                    prep.prepare({'query': query, 'consent': True})
                self.assertEqual((caught.exception.status, caught.exception.code), (422, code))
                self.assertNotIn('ИИ', caught.exception.message)
                gateway.extract.assert_not_called()
                google.resolve_place.assert_not_called()

    def test_ordinary_airport_visit_is_not_a_second_endpoint(self):
        prep, gateway, google = self.build('invalid_intent')
        response = prep.prepare({'query': 'Я около аэропорта, у меня 10 минут до полёта, хочу кофе', 'consent': True})
        self.assertFalse(response['needs_geolocation'])
        self.assertEqual(response['resolved_context']['minutes'], 10)
        self.assertEqual(response['resolved_context']['origin_id'], 'airport')
        gateway.extract.assert_called_once()
        google.resolve_place.assert_not_called()

    def test_invalid_point_to_point_form_is_still_rejected_before_any_extraction(self):
        prep, gateway, google = self.build('invalid_intent')
        with self.assertRaises(Exception) as caught:
            prep.prepare({'query': 'Хочу от Экспо до аэропорта', 'minutes': False, 'consent': True})
        self.assertEqual(caught.exception.code, 'invalid_request')
        gateway.extract.assert_not_called()
        google.resolve_place.assert_not_called()

    def test_denied_gps_resume_never_repeats_extraction_or_resolves_a_place(self):
        prep, gateway, google = self.build('invalid_intent')
        form = {'query': 'Дай парки в которые можно сходить ближайшие 2 часа рядом со мной', 'consent': True}
        first = prep.prepare(form)
        with self.assertRaises(Exception) as caught:
            prep.prepare({**form, 'preparation_token': first['preparation_token'], 'location_status': 'denied'})
        self.assertEqual(caught.exception.code, 'known_start_required')
        gateway.extract.assert_called_once()
        google.resolve_place.assert_not_called()


if __name__ == '__main__':
    unittest.main()
