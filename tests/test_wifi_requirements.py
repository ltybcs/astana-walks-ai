"""Wi-Fi parsing, own-source proof and unchanged whole narration; no actual network."""
from copy import deepcopy
import json
import unittest

from app.activity_intent import parse_search_intent
from app.amenity_fallback import relax_amenities,unconfirmed_amenities
from app.google_client import strict_json
from app.google_grounded import protected_claim_rejection,safe_intent
from app.place_evidence import normalize_place_evidence
from app.retrieval import Retriever,detect_intent,source_goal_evidence
from app.semantic_intent import deterministic_plan,validate_intent,extraction_request
from scripts.collect_2gis import normalize_items
from tests.test_goal_scoped_requirements import goal,request,requirement,place
from tests import test_unified_narration as fixtures


class WifiRequirementTests(unittest.TestCase):
    def test_previously_unrecognized_ru_en_wifi_variants_are_hard_not_a_new_category(self):
        queries=("Хочу кафе с Wi-Fi","Хочу кафе с Wi‑Fi","Хочу кафе с вайфаем",
                 "Хочу кафе с вай-фай","Хочу кафе с беспроводным интернетом",
                 "I want a cafe with wifi","I want a cafe with wireless internet")
        for query in queries:
            with self.subTest(query=query):
                search=parse_search_intent(query)
                self.assertTrue(search["supported"])
                self.assertEqual(search["categories"],["cafe"])
                self.assertIn("wifi",search["preferences"])
                plan=deterministic_plan(query)
                self.assertIn("wifi",plan.required_keys)
                self.assertTrue(detect_intent(query)["wifi"])
                self.assertTrue(plan.retrieval_intent()["wifi"])
                self.assertFalse(source_goal_evidence(place("cafe"),plan))
                self.assertTrue(source_goal_evidence(place("cafe",features={"wifi":"yes"}),plan))

    def test_negated_wifi_is_not_a_requirement_or_amenity_fallback(self):
        queries=("Хочу кафе без Wi-Fi","Хочу кафе, Wi-Fi не нужен", "Хочу кафе, вайфай необязателен",
                 "I want a cafe without wifi","I want a cafe, wifi is not required",
                 "I want a cafe but I don't need wifi","I want a cafe but I don’t need Wi-Fi")
        for query in queries:
            with self.subTest(query=query):
                self.assertNotIn("wifi",parse_search_intent(query)["preferences"])
                plan=deterministic_plan(query)
                self.assertNotIn("wifi",plan.required_keys)
                self.assertIsNone(relax_amenities(plan))

    def test_internet_club_and_laptop_are_not_wifi_evidence_or_requirement(self):
        query="Хочу интернет-клуб"
        plan=deterministic_plan(query)
        self.assertNotIn("wifi",plan.required_keys)
        self.assertFalse(detect_intent(query)["wifi"])
        requested=deterministic_plan("Хочу кафе с Wi-Fi")
        unknown=place("cafe",name="Wi-Fi cafe",description="Wi-Fi, тихо и можно с ноутбуком",features={"laptop":"yes"})
        self.assertFalse(source_goal_evidence(unknown,requested))
        self.assertEqual(unconfirmed_amenities(unknown,requested),("wifi",))

    def test_model_omission_legacy_and_scoped_projection_all_keep_hard_wifi(self):
        query="Хочу кафе с Wi-Fi или парк"
        model=request(goal("кафе","cafe"),goal("парк","park"))
        plan=validate_intent(model,query)
        self.assertEqual(plan.effective_required_keys(0),("wifi",))
        self.assertEqual(plan.effective_required_keys(1),())
        self.assertFalse(plan.retrieval_intent()["wifi"])
        self.assertFalse(source_goal_evidence(place("cafe"),plan))
        self.assertTrue(source_goal_evidence(place("park"),plan))
        self.assertEqual(safe_intent(query,intent_plan=plan)["preferences"],[])
        body=extraction_request(query,"synthetic-local")
        keys=body["response_format"]["json_schema"]["schema"]["properties"]["requirements"]["items"]["properties"]["key"]["enum"]
        self.assertIn("wifi",keys)
        global_plan=validate_intent(request(goal("кафе","cafe"),policy="primary",
            requirements=[{"quote":"Wi-Fi","key":"wifi","required":True}]),"Хочу кафе с Wi-Fi")
        self.assertTrue(global_plan.retrieval_intent()["wifi"])
        self.assertIn("wifi",safe_intent("unused",intent_plan=global_plan)["preferences"])

    def test_legacy_retriever_is_still_strict_without_an_alternate_workflow(self):
        records=[place("cafe",identifier="unknown"),place("cafe",identifier="yes",features={"wifi":"yes"}),
                 place("cafe",identifier="no",features={"wifi":"no"})]
        result=Retriever(data={"places":records}).retrieve("Хочу кафе с Wi-Fi",{"lat":51.0892,"lon":71.4074},60)
        self.assertEqual([r["poi"]["id"] for r in result["selected"]],["yes"])
        self.assertEqual(result["filtered"]["wifi"],2)

    def test_existing_dgis_wifi_label_has_own_normalized_proof_but_quiet_socket_stay_unknown(self):
        item={"id":"70000000000001","type":"branch","name":"Синтетическое кафе",
              "point":{"lat":51.0892,"lon":71.4074},"rubrics":[{"name":"Кафе"}],
              "attribute_groups":[{"attributes":[{"name":"Бесплатный Wi-Fi"},{"name":"Можно с ноутбуком"}]}]}
        original=deepcopy(item)
        poi=normalize_items([item],"2026-10-21T12:30:00Z")[0]
        projected=normalize_place_evidence(poi,provider="dgis",place_id=poi["id"],
            source_url=poi["source_url"],observed_at=poi["collected_at"])
        self.assertEqual(projected["features"]["wifi"],"yes")
        self.assertEqual(projected["features"]["socket"],"unknown")
        self.assertEqual(projected["features"]["quiet"],"unknown")
        evidence=next(e for e in projected["evidence"] if e["property"]=="wifi")
        self.assertEqual(evidence["place_id"],poi["id"])
        self.assertEqual(evidence["field_path"],"normalized.features.wifi")
        self.assertEqual(item,original)

    def test_google_summary_and_nonstandard_features_never_prove_wifi(self):
        google={"id":"synthetic-google","features":{"wifi":"yes"},"summary":"Wi-Fi available",
                "description":"Бесплатный Wi-Fi","rating":5}
        projected=normalize_place_evidence(google,provider="google",place_id=google["id"],
            source_url="https://www.google.com/maps?place_id=synthetic-google",observed_at="2026-10-21T12:30:00Z")
        self.assertEqual(projected["features"]["wifi"],"unknown")
        self.assertFalse(any(item["property"]=="wifi" for item in projected["evidence"]))

    def test_positive_wifi_claims_rejected_even_with_later_unrelated_advice(self):
        for text in ("В кафе есть Wi-Fi.","Кафе с вайфаем, но уточните часы.",
                     "Доступен беспроводной интернет.","У кафе есть интернет.",
                     "There is wireless internet.","Internet is available here.",
                     "В кафе есть доступ к интернету.","Можно пользоваться интернетом.",
                     "Доступ к интернету есть, но проверьте часы."):
            with self.subTest(text=text):
                self.assertIsNotNone(protected_claim_rejection(text))
        for text in ("Wi-Fi неизвестен.","Наличие вайфая не подтверждено.","Проверьте Wi-Fi.",
                     "Предлагаю посетить интернет-клуб.","Доступ к интернету неизвестен.",
                     "Наличие интернета не подтверждено.","Проверьте наличие интернета."):
            with self.subTest(text=text):
                self.assertIsNone(protected_claim_rejection(text))


class WifiNarrationProjectionTests(unittest.TestCase):
    setUp=fixtures.UnifiedNarrationTests.setUp
    model=fixtures.UnifiedNarrationTests.model
    run_narrator=fixtures.UnifiedNarrationTests.run_narrator

    def test_exact_own_name_with_internet_inflection_is_exempt_without_answer_rewrite(self):
        self.stops[0]["name"]="Интернетом"
        text="Предлагаю прогулку у места «Интернетом» по вашему интересу. Идея: сделать паузу. Условия посещения неизвестны."
        expected={**self.answer,"text":text}
        answer,status=self.run_narrator(http_open=self.model(expected))
        self.assertEqual(answer,expected)
        self.assertEqual(answer["text"],text)
        self.assertEqual(len(self.calls),1)

    def test_wifi_own_facts_reach_injected_model_without_modifying_full_google(self):
        self.stops[0]["features"]["wifi"]="unknown"
        self.stops[1]["features"]["wifi"]="yes"
        self.intent["preferences"].append("wifi")
        before=deepcopy((self.stops,self.grounding))
        answer,status=self.run_narrator()
        self.assertEqual(answer,self.answer)
        self.assertEqual(len(self.calls),1)
        context=strict_json(self.calls[0][0]["messages"][1]["content"])
        self.assertEqual(context["grounding_lite_summary"],self.grounding["summary"])
        self.assertEqual(len(context["all_google_source_previews"]),5)
        self.assertEqual(context["selected_provider_facts"][0]["features"]["wifi"],"unknown")
        self.assertEqual(context["selected_provider_facts"][1]["features"]["wifi"],"yes")
        self.assertEqual((self.stops,self.grounding),before)


if __name__=="__main__":unittest.main()
