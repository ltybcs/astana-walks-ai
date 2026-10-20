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
