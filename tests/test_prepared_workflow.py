"""One prepared semantic request reaches every source without lexical veto."""
from copy import deepcopy
from time import monotonic
import unittest
from unittest.mock import Mock

from app.google_budget import GoogleError
from app.lab_context import resolve_request, stage_request
from app.request_preparation import RequestPreparation
from app.route_map import RouteMap
from app.semantic_intent import IntentPlan, Goal
from app.unified_walks import UnifiedWalks
from tests.test_unified_walks import Google, poi


class PreparedWorkflowTests(unittest.TestCase):
    def build(self, query, plan, *, resolved_place=None):
        google=Google()
        if resolved_place is not None:
            google.resolve_place=Mock(return_value=[resolved_place])
        gateway=Mock()
        gateway.extract.return_value=(plan,'semantic',1)
        prep=RequestPreparation(resolve_request,lambda form,**kw:stage_request(form,probe=True,**kw),google,gateway)
        osm,dgis=Mock(),Mock()
        osm.retrieve.return_value={'selected':[{'poi':poi('osm:node/1','Парк',51.129,71.432,'park')}]}
        dgis.status.return_value={'available':True,'configured':True}
        club=poi('2gis:1','Клуб для теста',51.128,71.432,'computer_club')
        dgis.search.return_value={'places':[club],'warnings':[]}
        def narrate(_grounding,stops,*_):
            return {'text':'Игровая пауза по вашему пожеланию. Условия посещения неизвестны.',
                    'source_ids':[s['source_id'] for s in stops],'stop_ids':[s['stop_id'] for s in stops],
                    'venue_descriptions':[{'stop_id':s['stop_id'],'text':'Можно запланировать игровую паузу. Условия посещения стоит проверить.'} for s in stops]},'mock_validated'
        narrator=Mock(side_effect=narrate)
        service=UnifiedWalks(resolve_request,osm,google,dgis,preparation=prep,narrator=narrator,
                             route_map=RouteMap('PUBLIC_TEST_EMBED_KEY'))
        payload={'query':query,'time_of_day':'day','consent':True}
        response=prep.prepare(payload)
        payload['preparation_token']=response['preparation_token']
        return service,payload,prep,google,gateway,osm,dgis,narrator

    def test_unknown_wording_uses_one_plan_for_all_sources_and_one_answer(self):
        query='Хочу устроить лан-пати на два часа возле Байтерека'
        plan=IntentPlan('Байтерека',(Goal('лан-пати','компьютерные клубы','computer_club','venue_visit'),),(), 'primary',(),())
        service,payload,prep,google,gateway,osm,dgis,narrator=self.build(query,plan)
        result=service.recommend(payload)
        self.assertEqual(result['diagnostics']['intent_llm_attempts'],1)
        self.assertEqual(gateway.extract.call_count,1)
        self.assertEqual(google.search_count,1)
        self.assertIs(osm.retrieve.call_args.kwargs['intent_plan'],plan)
        self.assertIs(dgis.search.call_args.kwargs['intent_plan'],plan)
        self.assertEqual(narrator.call_count,1)
        stops=result['alternatives'][0]['stops']
        self.assertEqual([s['category'] for s in stops],['computer_club'])
        self.assertIn('игровую паузу',stops[0]['user_description'])
        self.assertEqual(len(google.legs),2)
        self.assertEqual(result['alternatives'][0]['budget_minutes'],120)

    def test_geocoded_coordinate_is_identical_in_outbound_return_and_map_ticket(self):
        query='Хочу устроить лан-пати на два часа возле Керуена'
        plan=IntentPlan('Керуена',(Goal('лан-пати','компьютерные клубы','computer_club','venue_visit'),),(), 'primary',(),())
        candidate={'id':'public-start','name':'Керуен','address':'Астана, Достык 9','lat':51.126123,'lon':71.427456,
                   'source_url':'https://www.google.com/maps/?cid=1','attributions':[]}
        service,payload,prep,google,gateway,osm,dgis,narrator=self.build(query,plan,resolved_place=candidate)
        result=service.recommend(payload)
        self.assertEqual(google.resolve_place.call_count,1)
        expected={'latLng':{'latitude':candidate['lat'],'longitude':candidate['lon']}}
        self.assertEqual(google.legs[0][0],expected)
        self.assertEqual(google.legs[-1][1],expected)
        option=result['alternatives'][0]
        mapped=service.route_map.resolve({'ticket':option['map_ticket']})
        from urllib.parse import urlsplit,parse_qs
        values=parse_qs(urlsplit(mapped['url']).query)
        self.assertEqual(values['origin'],values['destination'])
        self.assertIn('51.1261230',values['origin'][0])
        self.assertIn('71.4274560',values['origin'][0])

    def test_invalid_token_never_preflights_searches_or_narrates(self):
        query='Хочу лан-пати возле Байтерека'
        plan=IntentPlan('Байтерека',(Goal('лан-пати','компьютерные клубы','computer_club','venue_visit'),),(), 'primary',(),())
        service,payload,prep,google,gateway,osm,dgis,narrator=self.build(query,plan)
        payload['preparation_token']='invalid'
        with self.assertRaises(GoogleError):service.recommend(payload)
        self.assertEqual(google.search_count,0)
        self.assertEqual(len(google.legs),0)
        osm.retrieve.assert_not_called();dgis.search.assert_not_called();narrator.assert_not_called()


if __name__=='__main__':unittest.main()
