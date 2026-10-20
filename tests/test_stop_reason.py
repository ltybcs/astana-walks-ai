"""Card explanations use local verified choice facts, never provider prose."""
import unittest
from tests.test_category_unified import CLIENT, fixture, make_service
from app.lab_context import PUBLIC_ORIGINS
from app.unified_walks import _stop_reason

class StopReasonTests(unittest.TestCase):
    def test_real_orchestration_adds_a_specific_reason_without_echoing_private_text(self):
        origin=PUBLIC_ORIGINS["baiterek"]
        service, _, _, _=make_service(origin,[fixture(origin,"bowling")])
        result=service.recommend({"query":"Хочу боулинг возле Байтерека на два часа PRIVATE_MARKER", "client_context":CLIENT,"consent":True})
        option=result["alternatives"][0]
        reason=option["stops"][0]["why_text"]
        self.assertIn("боулинг",reason)
        self.assertIn("2ГИС",reason)
        self.assertIn("120 мин",reason)
        self.assertNotIn("PRIVATE_MARKER",reason)
        self.assertNotIn("computer_club",reason)
        self.assertNotIn("розет",reason)
        self.assertNotIn("тишин",reason)
        self.assertNotIn("свободн",reason)

    def test_unknown_features_and_unverified_route_never_become_card_promises(self):
        stop={"provider":"dgis", "category":"cafe", "features":{}, "opening_hours":None}
        reason=_stop_reason(stop,{"plan":{"verified":False},"route_built":False,"budget_minutes":60},
                            {"socket":True,"strict_quiet":True,"accessible":True,"night":True})
        for claim in ("розет", "тишин", "круглосуточ", "укладывается", "доступный вход"):
            self.assertNotIn(claim,reason)

    def test_own_explicit_features_can_explain_a_hard_condition(self):
        origin=PUBLIC_ORIGINS["baiterek"]
        service, _, _, _=make_service(origin,[fixture(origin,"cafe",features={"socket":"yes"})])
        result=service.recommend({"query":"Хочу кофе с розеткой возле Байтерека на час", "client_context":CLIENT,"consent":True})
        reason=result["alternatives"][0]["stops"][0]["why_text"]
        self.assertIn("розетки",reason)
        self.assertNotIn("тишина",reason)

    def test_exact_airport_pharmacy_question_never_substitutes_a_cafe(self):
        from tests.test_category_unified import resolve
        query="Не знаешь где поблизости к аэропорту есть аптека, у меня вылет через 2 часа. Я в Аэропорту"
        context=resolve({"query":query,"client_context":CLIENT})["resolved_context"]
        self.assertEqual(context["origin_id"],"airport")
        self.assertEqual(context["budget_options"],[120])
        origin=context["origin"]
        cafe=fixture(origin,"cafe",opening_hours="24/7")
        pharmacy=fixture(origin,"pharmacy","2gis:2",opening_hours="24/7")
        for available in ([cafe],[cafe,pharmacy]):
            with self.subTest(pharmacy=len(available)==2):
                service, google, _, narrator=make_service(origin,available)
                result=service.recommend({"query":query,"client_context":CLIENT,"consent":True})
                stops=result["alternatives"][0]["stops"]
                self.assertEqual([stop["category"] for stop in stops], ["pharmacy"] if len(available)==2 else [])
                self.assertEqual(narrator.call_count,int(len(available)==2))
                if stops:
                    self.assertIn("аптек",stops[0]["why_text"])
                    self.assertNotIn("кафе",stops[0]["why_text"])
                else:self.assertEqual(google.legs,[])

    def test_unknown_question_goal_does_not_start_a_generic_walk_search(self):
        from app.google_budget import GoogleError
        service, google, dgis, narrator=make_service(PUBLIC_ORIGINS["baiterek"],[])
        with self.assertRaises(GoogleError) as caught:
            service.recommend({"query":"Не знаешь где возле Байтерека есть планетарий", "client_context":CLIENT,"consent":True})
        self.assertEqual(caught.exception.code,"unsupported_activity")
        self.assertEqual(google.search_count,0)
        self.assertEqual(google.legs,[])
        dgis.search.assert_not_called()
        narrator.assert_not_called()
