"""Pure goal/condition association; all source records below are synthetic."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.semantic_intent import IntentError, validate_intent, deterministic_plan, extraction_request
from app.retrieval import Retriever, detect_intent, source_goal_evidence


def goal(quote, category, fulfilment="venue_visit"):
    return {"quote": quote, "search_text": quote, "category_hint": category, "fulfilment": fulfilment}


def request(*goals, policy="any", requirements=(), context=()):
    return {"start_quote": None, "goals": list(goals), "excluded": [], "goal_policy": policy,
            "requirements": list(requirements), "context": list(context)}


def requirement(quote, key, index=None, required=True):
    return {"quote": quote, "key": key, "required": required, "goal_index": index}


def place(category, *, features=None, identifier=None, **extra):
    return {"id": identifier or category, "name": "Синтетический " + (identifier or category),
            "category": category, "description": "Синтетическое исходное наблюдение.",
            "lat": 51.0895, "lon": 71.4077, "features": features or {}, "opening_hours": "24/7",
            "visit_minutes": 15, "source_url": "https://example.test/source", **extra}


class GoalScopedRequirementTests(unittest.TestCase):
    def test_socket_and_type_must_match_same_or_branch_actual_retriever(self):
        query = "Хочу кафе с розеткой или парк"
        value = request(goal("кафе", "cafe"), goal("парк", "park"),
                        requirements=[requirement("розеткой", "socket", 0)])
        original = deepcopy(value)
        plan = validate_intent(value, query)
        self.assertEqual(plan.required_keys, ("socket",))
        self.assertEqual(plan.global_required_keys, ())
        self.assertEqual(plan.effective_required_keys(0), ("socket",))
        self.assertEqual(plan.effective_required_keys(1), ())
        self.assertFalse(plan.retrieval_intent()["socket"])
        self.assertFalse(detect_intent(query, intent_plan=plan)["socket"])
        self.assertEqual(detect_intent(query, intent_plan=plan)["required_keys"], [])
        records = [place("cafe", identifier="unknown-cafe"),
                   place("cafe", identifier="yes-cafe", features={"socket": "yes"}),
                   place("park", identifier="unknown-park")]
        retriever = Retriever(data={"places": records})
        result = retriever.retrieve(query, {"lat": 51.0892, "lon": 71.4074}, 120, intent_plan=plan)
        self.assertEqual({r["poi"]["id"] for r in result["selected"]}, {"yes-cafe", "unknown-park"})
        self.assertEqual(result["retrieved_count"], 2)
        self.assertEqual(value, original)
        self.assertEqual(plan.as_dict()["requirements"][0]["goal_index"], 0)

    def test_interactive_museum_does_not_discard_playground(self):
        query = "Я с ребёнком, хочу детские площадки или музей с интерактивами"
        plan = validate_intent(request(goal("детские площадки", "playground"),
                                       goal("музей с интерактивами", "museum", "specific_activity")), query)
        self.assertEqual(plan.effective_required_keys(0), ())
        self.assertEqual(plan.effective_required_keys(1), ("activity",))
        self.assertEqual(plan.global_required_keys, ())
        self.assertTrue(plan.retrieval_intent()["child"])
        self.assertTrue(source_goal_evidence(place("playground"), plan))
        self.assertFalse(source_goal_evidence(place("museum"), plan))
        self.assertFalse(source_goal_evidence(place("park"), plan))
        # A source type or source prose cannot establish the missing service.
        self.assertFalse(source_goal_evidence(place("museum", description="Интерактивный музей"), plan))

    def test_global_wheelchair_and_child_remain_hard_for_all_branches(self):
        query = "Я с ребёнком, на коляске, хочу кафе или парк"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park")), query)
        self.assertEqual(plan.global_required_keys, ("accessible",))
        self.assertTrue(plan.retrieval_intent()["child"])
        self.assertTrue(plan.retrieval_intent()["accessible"])
        for category in ("park", "cafe"):
            with self.subTest(category=category):
                self.assertFalse(source_goal_evidence(place(category), plan))
                self.assertTrue(source_goal_evidence(place(category, features={"wheelchair": "yes"}), plan))
        query = "Я с ребёнком, хочу кафе или ночной клуб"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("ночной клуб", "nightclub")), query)
        result = Retriever(data={"places": [place("cafe"), place("nightclub")]}).retrieve(
            query, {"lat": 51.0892, "lon": 71.4074}, 120, intent_plan=plan)
        self.assertEqual([r["poi"]["category"] for r in result["selected"]], ["cafe"])

    def test_primary_uses_goal_zero_even_when_primary_is_walk(self):
        query = "Главная цель парк, ещё интересует музей с интерактивами"
        plan = validate_intent(request(goal("парк", "park"),
                                       goal("музей с интерактивами", "museum", "specific_activity"),
                                       policy="primary"), query)
        self.assertEqual(plan.mode, "walk")
        self.assertEqual(plan.search_intent["categories"], ["park", "museum"])
        self.assertEqual(plan.retrieval_intent()["preferred_categories"], ["park"])
        self.assertEqual(plan.source_phrase, "Парки и сады")
        self.assertEqual(plan.search_text, "парк")
        self.assertTrue(source_goal_evidence(place("park"), plan))
        self.assertFalse(source_goal_evidence(place("museum"), plan))

    def test_legacy_three_field_is_global_new_null_can_follow_literal_local_scope(self):
        query = "Хочу кафе с розеткой или парк"
        goals = (goal("кафе", "cafe"), goal("парк", "park"))
        legacy = {"quote": "розеткой", "key": "socket", "required": True}
        plan = validate_intent(request(*goals, requirements=[legacy]), query)
        self.assertEqual(plan.global_required_keys, ("socket",))
        self.assertFalse(source_goal_evidence(place("park"), plan))
        modern = validate_intent(request(*goals, requirements=[requirement("розеткой", "socket")]), query)
        self.assertEqual(modern.global_required_keys, ())
        self.assertEqual(modern.requirements[0].goal_index, 0)
        self.assertTrue(source_goal_evidence(place("park"), modern))
        roundtrip = validate_intent(modern.as_dict(), query)
        self.assertEqual(roundtrip.as_dict(), modern.as_dict())

    def test_schema_is_four_field_and_sanitized_has_index_not_quotes(self):
        query = "Хочу кафе с розеткой или парк"
        body = extraction_request(query, "synthetic-local-model")
        schema = body["response_format"]["json_schema"]["schema"]["properties"]["requirements"]["items"]
        self.assertEqual(set(schema["required"]), {"quote", "key", "required", "goal_index"})
        self.assertFalse(schema["additionalProperties"])
        self.assertIn({"type": "null"}, schema["properties"]["goal_index"]["anyOf"])
        plan = deterministic_plan(query)
        self.assertEqual(plan.sanitized_narration["requirements"], [{"key": "socket", "required": True, "goal_index": 0}])
        self.assertNotIn("quote", json.dumps(plan.sanitized_narration))
        self.assertEqual(body["max_tokens"], 512)
        self.assertIs(body["cache_prompt"], False)

    def test_bad_or_unproven_model_indices_cannot_relax_a_condition(self):
        query = "Хочу кафе с розеткой или парк"
        for index in (-1, 2, True, 0.0, "0", [], {}):
            with self.subTest(index=index), self.assertRaises(IntentError):
                validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                        requirements=[requirement("розеткой", "socket", index)]), query)
        with self.assertRaises(IntentError):
            validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                    requirements=[requirement("розеткой", "socket", 1)]), query)
        for query, quote in (("Хочу кафе или парк. Везде нужна розетка", "розетка"),
                             ("Хочу кафе или парк с розеткой для всех вариантов", "розеткой"),
                             ("Нужна розетка, хочу кафе или парк", "розетка"),
                             ("Хочу кафе или парк, обязательно с розеткой", "розеткой")):
            with self.subTest(query=query), self.assertRaises(IntentError):
                validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                        requirements=[requirement(quote, "socket", 1)]), query)

    def test_non_or_and_overlapping_branches_cannot_localize(self):
        query = "Хочу кафе с розеткой и парк"
        with self.assertRaises(IntentError):
            validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"), policy="primary",
                                    requirements=[requirement("розеткой", "socket", 0)]), query)
        query = "Хочу кафе с розеткой или парк"
        with self.assertRaises(IntentError):
            validate_intent(request(goal("кафе с розеткой или парк", "cafe"), goal("парк", "park"),
                                    requirements=[requirement("розеткой", "socket", 0)]), query)

    def test_local_key_cannot_hide_later_global_witness_with_same_key(self):
        query = "Хочу кафе с розеткой или парк. Для всех вариантов обязательна розетка"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                       requirements=[requirement("розеткой", "socket", 0)]), query)
        self.assertEqual({r.goal_index for r in plan.requirements if r.key == "socket" and r.required}, {0, None})
        self.assertTrue(plan.retrieval_intent()["socket"])
        self.assertFalse(source_goal_evidence(place("park"), plan))
        self.assertTrue(source_goal_evidence(place("park", features={"socket": "yes"}), plan))
        # A broad quote containing both scopes must not be forced local.
        with self.assertRaises(IntentError):
            validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                    requirements=[requirement(query, "socket", 0)]), query)

    def test_repeated_same_literal_witnesses_preserve_all_scopes(self):
        query = "Хочу кафе с розеткой или парк. Везде с розеткой"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park")), query)
        self.assertEqual({r.goal_index for r in plan.requirements}, {0, None})
        self.assertEqual(validate_intent(plan.as_dict(), query).as_dict(), plan.as_dict())

    def test_negative_witness_does_not_create_global_or_local_demand(self):
        query = "Хочу кафе или парк. Розетки не нужны"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park")), query)
        self.assertEqual(plan.required_keys, ())
        self.assertTrue(source_goal_evidence(place("park"), plan))
        with self.assertRaises(IntentError):
            validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                    requirements=[requirement("Розетки", "socket", 1)]), query)

    def test_deterministic_or_uses_same_local_scope_and_keeps_interactive_service(self):
        with patch("socket.create_connection", side_effect=AssertionError("No network")):
            cafe = deterministic_plan("Хочу кафе с розеткой или парк")
            family = deterministic_plan("Я с ребёнком, хочу детские площадки или музей с интерактивами")
        self.assertEqual(cafe.goal_policy, "any")
        self.assertEqual(cafe.requirements[0].goal_index, 0)
        self.assertTrue(source_goal_evidence(place("park"), cafe))
        self.assertFalse(source_goal_evidence(place("cafe"), cafe))
        self.assertTrue(source_goal_evidence(place("playground"), family))
        self.assertFalse(source_goal_evidence(place("museum"), family))
        self.assertEqual(family.effective_required_keys(1), ("activity",))

    def test_soft_quiet_preference_is_not_promoted_or_applied_to_unrelated_branch(self):
        query = "Хочу тихое кафе или парк"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                       requirements=[requirement("тихое", "quiet", 0, False)]), query)
        self.assertEqual(plan.preferences, ("quiet",))
        self.assertEqual(plan.global_preferences, ())
        self.assertFalse(plan.retrieval_intent()["quiet"])
        self.assertTrue(source_goal_evidence(place("park"), plan))
        self.assertTrue(source_goal_evidence(place("cafe"), plan))
        hard_query = "Хочу только тихое кафе или парк"
        hard = deterministic_plan(hard_query)
        self.assertEqual(hard.effective_required_keys(0), ("quiet",))
        self.assertEqual(hard.effective_required_keys(1), ())
        self.assertFalse(source_goal_evidence(place("cafe"), hard))
        self.assertTrue(source_goal_evidence(place("park"), hard))

    def test_sanitized_legacy_and_corrupted_scope_fail_closed(self):
        plan = deterministic_plan("Хочу кафе с розеткой или парк")
        projected = detect_intent("unused", intent_plan=plan)
        for bad in (-1, 2, True, "0"):
            changed = deepcopy(projected)
            changed["semantic_goal"]["requirements"][0]["goal_index"] = bad
            with self.subTest(bad=bad):
                self.assertFalse(source_goal_evidence(place("park"), changed))
        legacy = deepcopy(projected)
        del legacy["semantic_goal"]["requirements"][0]["goal_index"]
        self.assertFalse(source_goal_evidence(place("park"), legacy))

    def test_two_conditions_cannot_be_borrowed_across_branches_or_places(self):
        query = "Хочу кафе с розеткой или парк с доступом для коляски"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"),
                                       requirements=[requirement("розеткой", "socket", 0),
                                                     requirement("коляски", "accessible", 1)]), query)
        self.assertFalse(source_goal_evidence(place("cafe", features={"wheelchair": "yes"}), plan))
        self.assertFalse(source_goal_evidence(place("park", features={"socket": "yes"}), plan))
        self.assertTrue(source_goal_evidence(place("park", features={"wheelchair": "yes"}), plan))
        self.assertTrue(source_goal_evidence(place("cafe", features={"socket": "yes"}), plan))

    def test_hard_global_activity_equipment_inventory_price_stay_unproven(self):
        cases = (("оборудование", "equipment"), ("товар в наличии", "inventory"), ("цена", "price"),
                 ("погладить капибару", "activity"))
        for phrase, key in cases:
            query = "Хочу кафе или парк. Обязательно " + phrase
            value = request(goal("кафе", "cafe"), goal("парк", "park"),
                            requirements=[requirement(phrase, key)])
            with self.subTest(key=key):
                plan = validate_intent(value, query)
                self.assertIn(key, plan.global_required_keys)
                self.assertFalse(source_goal_evidence(place("cafe"), plan))
                self.assertFalse(source_goal_evidence(place("park"), plan))


    def test_model_venue_label_cannot_erase_explicit_interactive_witness(self):
        query = "Хочу детские площадки или музей с интерактивами"
        plan = validate_intent(request(goal("детские площадки", "playground"), goal("музей", "museum")), query)
        self.assertEqual(plan.effective_required_keys(0), ())
        self.assertEqual(plan.effective_required_keys(1), ("activity",))
        self.assertTrue(source_goal_evidence(place("playground"), plan))
        self.assertFalse(source_goal_evidence(place("museum"), plan))
        global_query = query + ". Во всех вариантах нужны интерактивы"
        global_plan = validate_intent(request(goal("детские площадки", "playground"), goal("музей", "museum")), global_query)
        self.assertEqual(global_plan.global_required_keys, ("activity",))
        self.assertFalse(source_goal_evidence(place("playground"), global_plan))

    def test_broad_walk_or_club_evaluates_each_type_without_global_outdoor_filter(self):
        query = "Хочу погулять или компьютерный клуб"
        plan = validate_intent(request(goal("погулять", "walk", "generic_walk"),
                                       goal("компьютерный клуб", "computer_club")), query)
        records = [place("park"), place("computer_club"), place("bar"), place("mall")]
        result = Retriever(data={"places": records}).retrieve(
            query, {"lat": 51.0892, "lon": 71.4074}, 120, intent_plan=plan)
        self.assertEqual({r["poi"]["category"] for r in result["selected"]}, {"park", "computer_club"})
        for category in ("bar", "mall"):
            self.assertFalse(source_goal_evidence(place(category), plan))
        broad = validate_intent(request(goal("погулять", "walk", "generic_walk"), policy="primary"), "Хочу погулять")
        self.assertFalse(source_goal_evidence(place("museum"), broad))
        tourist = validate_intent(request(goal("погулять", "walk", "generic_walk"), policy="primary"),
                                  "Я турист, хочу погулять")
        self.assertTrue(source_goal_evidence(place("museum"), tourist))
        self.assertFalse(source_goal_evidence(place("bar"), tourist))

    def test_architecture_proof_is_own_structured_type_not_museum_or_source_prose(self):
        query = "Хочу архитектуру или парк"
        plan = validate_intent(request(goal("архитектуру", "architecture"), goal("парк", "park")), query)
        wrong = place("museum", name="Исторический дворец", description="Архитектурная достопримечательность")
        proven = place("museum", identifier="proven", source_tags={"historic": "building"})
        self.assertFalse(source_goal_evidence(wrong, plan))
        self.assertTrue(source_goal_evidence(proven, plan))
        self.assertTrue(source_goal_evidence(place("monument"), plan))
        self.assertTrue(source_goal_evidence(place("park"), plan))
        for key in ("building:architecture", "architectural_style"):
            self.assertTrue(source_goal_evidence(place("theatre", source_tags={key: "source-reported-style"}), plan))
            self.assertFalse(source_goal_evidence(place("theatre", source_tags={key: "unknown"}), plan))
        result = Retriever(data={"places": [wrong, proven, place("park")]}).retrieve(
            query, {"lat": 51.0892, "lon": 71.4074}, 120, intent_plan=plan)
        self.assertEqual({r["poi"]["id"] for r in result["selected"]}, {"proven", "park"})
        museum_or_arch = validate_intent(request(goal("музей", "museum"), goal("архитектуру", "architecture")),
                                         "Хочу музей или архитектуру")
        self.assertTrue(source_goal_evidence(wrong, museum_or_arch))

    def test_three_or_branches_keep_original_goal_indices(self):
        query = "Хочу кафе с розеткой или парк или музей с интерактивами"
        plan = validate_intent(request(goal("кафе", "cafe"), goal("парк", "park"), goal("музей", "museum")), query)
        self.assertEqual(plan.effective_required_keys(0), ("socket",))
        self.assertEqual(plan.effective_required_keys(1), ())
        self.assertEqual(plan.effective_required_keys(2), ("activity",))
        self.assertTrue(source_goal_evidence(place("park"), plan))
        self.assertFalse(source_goal_evidence(place("cafe"), plan))
        self.assertFalse(source_goal_evidence(place("museum"), plan))


    def test_deterministic_walk_choice_is_not_erased_or_invented_for_a_known_venue(self):
        for query in ("Хочу погулять или компьютерный клуб", "Хочу компьютерный клуб или погулять"):
            with self.subTest(query=query):
                plan=deterministic_plan(query)
                self.assertEqual(plan.goal_policy,"any")
                self.assertEqual(set(plan.search_intent["categories"]),{"walk","computer_club"})
                self.assertTrue(source_goal_evidence(place("park"),plan))
                self.assertTrue(source_goal_evidence(place("computer_club"),plan))
                self.assertFalse(source_goal_evidence(place("bar"),plan))
        with self.assertRaises(IntentError):
            deterministic_plan("Хочу пострелять из арбалета или компьютерный клуб")
        # Without an actual OR, a secondary generic phrase cannot change primary.
        ordinary=deterministic_plan("Хочу компьютерный клуб и погулять")
        self.assertEqual(ordinary.goal_policy,"primary")
        self.assertEqual(ordinary.canonical_primary,"computer_club")

    def test_only_prevalidated_google_untyped_broad_path_preserved(self):
        plan=deterministic_plan("Хочу погулять")
        preview=place("unknown",provider="google",id="opaque-google",stop_id="google:opaque-google",source_id="g2")
        self.assertTrue(source_goal_evidence(preview,plan))
        for changed in (dict(preview,provider="osm"),dict(preview,source_id="o2"),
                        dict(preview,stop_id="google:other"),dict(preview,category="bar"),dict(preview,category="mall")):
            self.assertFalse(source_goal_evidence(changed,plan))
        self.assertFalse(source_goal_evidence(preview,detect_intent("unused","night",intent_plan=plan)))
        for query in ("Я с ребёнком, хочу погулять", "Хочу погулять, нужна розетка", "Хочу в парк"):
            with self.subTest(query=query):
                self.assertFalse(source_goal_evidence(preview,deterministic_plan(query)))


    def test_explicit_priority_controls_deterministic_and_model_policy_even_for_walk_type(self):
        query="Главная цель парк, ещё интересует музей"
        plan=deterministic_plan(query)
        self.assertEqual(plan.mode,"walk")
        self.assertEqual(plan.goal_policy,"primary")
        self.assertTrue(source_goal_evidence(place("park"),plan))
        self.assertFalse(source_goal_evidence(place("museum"),plan))
        with self.assertRaises(IntentError):
            validate_intent(request(goal("парк","park"),goal("музей","museum"),policy="walk_mix"),query)
        # Explicit priority also dominates optional OR for walk categories.
        optional="Главная цель парк или музей"
        for primary in (deterministic_plan(optional),
                        validate_intent(request(goal("парк","park"),goal("музей","museum"),policy="primary"),optional)):
            self.assertEqual(primary.goal_policy,"primary")
            self.assertTrue(source_goal_evidence(place("park"),primary))
            self.assertFalse(source_goal_evidence(place("museum"),primary))
        for policy in ("any","walk_mix"):
            with self.subTest(policy=policy), self.assertRaises(IntentError):
                validate_intent(request(goal("парк","park"),goal("музей","museum"),policy=policy),optional)
        # A normal multi-interest walk keeps its existing mixture semantics.
        ordinary=deterministic_plan("Хочу парк и музей")
        self.assertEqual(ordinary.goal_policy,"walk_mix")
        self.assertTrue(source_goal_evidence(place("museum"),ordinary))


if __name__ == "__main__":
    unittest.main()
