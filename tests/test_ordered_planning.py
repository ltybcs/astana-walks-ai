from copy import deepcopy
import unittest

import test_unified_walks as fixtures
from app.semantic_intent import deterministic_plan
from app.ordered_goals import ordered_goals
from app.unified_walks import _stop


class OrderedPlanningTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnifiedTests.setUp(self)
        self.addCleanup(self.map.close)
        self.query='Я в Байтереке, хочу пойти в кафе и потом прогуляться вокруг набережной или какого либо парка'
        self.plan,self.groups=ordered_goals(self.query,deterministic_plan(self.query))
        self.cafe1=fixtures.poi('osm:node/100','Синтетическое кафе Альфа',51.1285,71.4307,'cafe')
        self.cafe2=fixtures.poi('osm:node/101','Синтетическое кафе Бета',51.129,71.431,'cafe')
        self.park1=fixtures.poi('osm:node/102','Синтетический парк Альфа',51.130,71.432,'park')
        self.park2=fixtures.poi('osm:node/103','Синтетический парк Бета',51.131,71.433,'park')
        self.river=fixtures.poi('osm:node/104','Синтетическая набережная',51.132,71.434,'promenade')
        self.dgis.search.return_value={'places': [], 'warnings': []}

    def recommend(self,places=None,query=None):
        self.osm.retrieve.return_value={'selected':[{'poi':deepcopy(place)} for place in
            ([self.cafe1,self.cafe2,self.park1,self.park2,self.river] if places is None else places)]}
        return self.service.recommend({'query':query or self.query,'time_of_day':'day','consent':True})

    def test_three_budgets_use_distinct_complete_stage_preserving_routes(self):
        result=self.recommend()
        identities=[]
        for option in result['alternatives']:
            self.assertTrue(option['route_built'])
            self.assertTrue(option['plan']['ordered_goals_covered'])
            first,second=option['stops']
            self.assertEqual(first['category'],'cafe')
            self.assertIn(second['category'],{'park','promenade'})
            self.assertLessEqual(option['plan']['total_minutes'],option['budget_minutes'])
            self.assertEqual(option['plan']['legs'][0]['from_ref'],'origin')
            self.assertEqual(option['plan']['legs'][-1]['to_ref'],'origin')
            identities.append(tuple(stop['stop_id'] for stop in option['stops']))
        self.assertEqual(len(set(identities)),3)
        self.assertLessEqual(len(self.google.legs),11)
        self.assertEqual(self.google.search_count,1)
        self.assertEqual(self.dgis.search.call_count,1)

    def test_one_complete_route_is_reused_honestly_not_filled_with_other_types(self):
        result=self.recommend([self.cafe1,self.park1])
        identities=[tuple(stop['stop_id'] for stop in option['stops']) for option in result['alternatives']]
        self.assertEqual(len(set(identities)),1)
        self.assertNotIn('route_note',result['alternatives'][0])
        self.assertTrue(all('тот же путь' in option['route_note'] for option in result['alternatives'][1:]))

    def test_missing_either_stage_never_claims_complete_sequence(self):
        for places in ([self.cafe1],[self.park1,self.river]):
            with self.subTest(categories=[place['category'] for place in places]):
                result=self.recommend(places)
                self.assertTrue(all(not option['route_built'] and not option['stops'] for option in result['alternatives']))
                self.assertTrue(any('Полный план' in note for note in result['warnings']))

    def test_known_closed_stage_is_not_replaced_by_a_cafe_only_route(self):
        self.park1['opening_hours']='Mo 00:00-00:01'
        # Explicit synthetic Wednesday instant, no current machine-time assumptions.
        from datetime import datetime,timezone
        self.service.clock=lambda:datetime(2026,10,21,7,tzinfo=timezone.utc)
        self.osm.retrieve.return_value={'selected':[{'poi':self.cafe1},{'poi':self.park1}]}
        result=self.service.recommend({'query':self.query,'consent':True})
        self.assertTrue(all(not option['route_built'] for option in result['alternatives']))

    def test_unknown_cafe_socket_fallback_keeps_strict_outdoor_sibling(self):
        result=self.recommend([self.cafe1,self.park1],query='Я у Байтерека, хочу кафе с розеткой, потом парк')
        for option in result['alternatives']:
            self.assertTrue(option['route_built'])
            self.assertEqual([stop['category'] for stop in option['stops']],['cafe','park'])
            self.assertEqual(option['amenity_fallback']['unconfirmed'],['socket'])
            self.assertEqual(option['stops'][0]['unconfirmed_amenities'],['socket'])
            self.assertNotIn('unconfirmed_amenities',option['stops'][1])
            self.assertNotIn('socket',option['stops'][1]['requested_features'])

    def test_named_exclusion_filters_before_routing_without_removing_other_parks(self):
        result=self.recommend([self.cafe1,self.park1,self.park2],
            query=self.query+'. Не предлагай «Синтетический парк Альфа».')
        for option in result['alternatives']:
            self.assertTrue(option['route_built'])
            self.assertNotIn(self.park1['id'],[stop['id'] for stop in option['stops']])
            self.assertIn(self.park2['id'],[stop['id'] for stop in option['stops']])
        self.assertEqual(result['source_receipt']['osm']['excluded_named_count'],1)
        self.assertNotIn('Синтетический парк Альфа',repr(self.narrator.call_args.args[1]))


if __name__=='__main__':
    unittest.main()
