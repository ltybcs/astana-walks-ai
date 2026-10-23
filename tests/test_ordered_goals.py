"""Pure literal activity ordering; no provider, model, storage or planner calls."""
from copy import deepcopy
from dataclasses import replace
import unittest

from app.ordered_goals import ordered_goals
from app.retrieval import source_goal_evidence
from app.semantic_intent import ContextFlag, ExcludedGoal, Goal, IntentPlan, Requirement, deterministic_plan


def plan_for(*quotes, policy="primary", **kwargs):
    categories = {"кафе": "cafe", "кафе с розеткой": "cafe", "парк": "park", "парка": "park", "парке": "park", "набережной": "promenade",
                  "музей": "museum", "ресторан": "restaurant", "cafe": "cafe", "park": "park", "river": "promenade"}
    return IntentPlan(None, tuple(Goal(quote, quote, categories.get(quote), "venue_visit") for quote in quotes),
                      kwargs.get("excluded", ()), policy, kwargs.get("requirements", ()), kwargs.get("context", ()),
                      query_sha256="synthetic-input-hash")


class OrderedGoalsTests(unittest.TestCase):
    def test_tester_phrase_has_mandatory_cafe_then_outdoor_choice(self):
        query = "Я в Байтереке, хочу пойти в кафе и потом прогуляться вокруг набережной или какого либо парка"
        original = deterministic_plan(query)
        result, groups = ordered_goals(query, original)
        self.assertEqual([goal.category_hint for goal in result.goals], ["cafe", "promenade", "park"])
        self.assertEqual(groups, ((0,), (1, 2)))
        self.assertEqual(result.goal_policy, "walk_mix")

    def test_literal_sequence_can_promote_primary_or_any_without_mutation(self):
        for policy in ("primary", "any", "walk_mix"):
            with self.subTest(policy=policy):
                query = "Хочу кафе, затем парк или музей"
                original = plan_for("кафе", "парк", "музей", policy=policy)
                snapshot = deepcopy(original)
                result, groups = ordered_goals(query, original)
                self.assertEqual(groups, ((0,), (1, 2)))
                self.assertEqual(result.goal_policy, "walk_mix")
                self.assertEqual(original, snapshot)
                self.assertEqual(result.goals, original.goals)
                if policy == "walk_mix":
                    self.assertIs(result, original)
                else:
                    self.assertIsNot(result, original)

    def test_english_and_single_suffix_goal_keep_literal_indices(self):
        for query, original in (("A cafe then walk around a park", plan_for("cafe", "park")),
                                ("Хочу кафе, а затем погулять в парке", plan_for("кафе", "парке"))):
            with self.subTest(query=query):
                result, groups = ordered_goals(query, original)
                self.assertEqual(groups, ((0,), (1,)))
                self.assertEqual(result.goal_policy, "walk_mix")
        reversed_indices = plan_for("park", "cafe", policy="walk_mix")
        _, groups = ordered_goals("cafe then park", reversed_indices)
        self.assertEqual(groups, ((1,), (0,)))

    def test_all_original_constraints_and_metadata_survive(self):
        query = "Я в Байтереке с ребёнком, нужен вход для коляски. Не хочу бары. Хочу кафе с розеткой, потом парк"
        original = plan_for("кафе с розеткой", "парк",
            excluded=(ExcludedGoal("бары", "bar"),),
            requirements=(Requirement("розеткой", "socket", True, 0),
                          Requirement("коляски", "accessible", True, None)),
            context=(ContextFlag("ребёнком", "child"),))
        original = replace(original, start_quote="Байтереке", extraction_mode="semantic")
        before = deepcopy(original)
        result, groups = ordered_goals(query, original)
        self.assertEqual(groups, ((0,), (1,)))
        self.assertEqual(original, before)
        for name in ("goals", "requirements", "excluded", "context", "start_quote", "query_sha256", "extraction_mode"):
            self.assertEqual(getattr(result, name), getattr(original, name))
        self.assertEqual([item.goal_index for item in result.requirements], [0, None])

    def test_explicit_primary_priority_is_never_overridden(self):
        for prefix in ("Главное ", "Главная цель ", "Основная цель ", "Main goal ", "Primary goal "):
            query = prefix + "кафе, потом парк"
            original = plan_for("кафе", "парк")
            result, groups = ordered_goals(query, original)
            self.assertIs(result, original)
            self.assertEqual(groups, ())

    def test_first_stage_explicit_convenience_quotes_are_preserved_and_locally_consumed(self):
        cases = (
            ("Хочу кафе с розеткой, потом парк", "кафе", "парк",
             (Requirement("розеткой", "socket", True, 0),)),
            ("cafe with wifi then park", "cafe", "park",
             (Requirement("wifi", "wifi", True, 0),)),
            ("Хочу кафе с розеткой и Wi-Fi, затем парк", "кафе", "парк",
             (Requirement("розеткой", "socket", True, 0), Requirement("Wi-Fi", "wifi", True, 0))),
        )
        for query, first, second, requirements in cases:
            with self.subTest(query=query):
                original = plan_for(first, second, requirements=requirements)
                before = deepcopy(original)
                result, groups = ordered_goals(query, original)
                self.assertEqual(groups, ((0,), (1,)))
                self.assertEqual(result.requirements, requirements)
                self.assertEqual(original, before)

    def test_closed_first_stage_with_witness_localizes_only_existing_global_convenience(self):
        query = "Я у Байтерека хочу кафе с розеткой, потом парк"
        original = deterministic_plan(query)
        self.assertIn("socket", original.global_required_keys)
        before = deepcopy(original)
        result, groups = ordered_goals(query, original)
        self.assertEqual(groups, ((0,), (1,)))
        self.assertEqual(original, before)
        self.assertEqual(result.effective_required_keys(0), ("socket",))
        self.assertEqual(result.effective_required_keys(1), ())
        self.assertTrue(source_goal_evidence({"category": "park", "features": {}}, result))
        self.assertFalse(source_goal_evidence({"category": "cafe", "features": {}}, result))
        self.assertTrue(source_goal_evidence({"category": "cafe", "features": {"socket": "yes"}}, result))
        for old, new in zip(original.requirements, result.requirements):
            self.assertEqual((old.quote, old.key, old.required), (new.quote, new.key, new.required))
            if old.key == "socket":
                self.assertEqual(new.goal_index, 0)

    def test_explicit_global_or_independent_witness_never_narrows(self):
        for prefix in ("Везде нужны розетки. ", "В обоих местах нужна розетка. ",
                       "Both places need sockets. ", "Розетка нужна. "):
            query = prefix + "Хочу кафе с розеткой, потом парк"
            original = plan_for("кафе", "парк", requirements=(Requirement("розеткой", "socket", True),))
            result, groups = ordered_goals(query, original)
            self.assertEqual(groups, ((0,), (1,)))
            self.assertEqual(result.requirements, original.requirements)
            self.assertEqual(result.global_required_keys, ("socket",))

    def test_no_with_witness_and_accessibility_stay_global(self):
        cases = (("Хочу кафе, нужна розетка, потом парк", Requirement("нужна розетка", "socket", True)),
                 ("Хочу кафе с коляской, потом парк", Requirement("коляской", "accessible", True)))
        for query, requirement in cases:
            original = plan_for("кафе", "парк", requirements=(requirement,))
            result, groups = ordered_goals(query, original)
            self.assertEqual(groups, ((0,), (1,)))
            self.assertEqual(result.requirements, (requirement,))

    def test_named_exclusion_casefold_expansion_does_not_move_goal_offsets(self):
        query = "I've visited «Straße». cafe then park"
        result, groups = ordered_goals(query, plan_for("cafe", "park"))
        self.assertEqual(groups, ((0,), (1,)))
        self.assertEqual(result.goal_policy, "walk_mix")
        # A source-owned title mask needs its original capitalization even
        # though goal quote matching uses the normalized lowercase request.
        query = "cafe then park. Не предлагай Звёздный сквер"
        _, groups = ordered_goals(query, plan_for("cafe", "park"))
        self.assertEqual(groups, ((0,), (1,)))

    def test_other_stage_condition_or_unknown_content_in_a_feature_quote_cannot_be_erased(self):
        cases = (
            ("Хочу кафе с розеткой, потом парк", Requirement("розеткой", "socket", True, 1)),
            ("Хочу кафе с Wi-Fi и погладить дракона, потом парк",
             Requirement("Wi-Fi и погладить дракона", "wifi", True, 0)),
            ("Хочу кафе с розеткой и неизвестной услугой, потом парк",
             Requirement("розеткой", "socket", True, 0)),
            ("Хочу кафе без Wi-Fi, потом парк", Requirement("Wi-Fi", "wifi", True, 0)),
        )
        for query, requirement in cases:
            with self.subTest(query=query):
                original = plan_for("кафе", "парк", requirements=(requirement,))
                result, groups = ordered_goals(query, original)
                self.assertIs(result, original)
                self.assertEqual(groups, ())

    def test_negated_stage_or_connector_does_not_create_a_sequence(self):
        for query in ("Не хочу кафе, потом парк", "Хочу кафе, не потом парк",
                      "Хочу кафе, потом не парк", "Хочу кафе, потом парк не нужен"):
            with self.subTest(query=query):
                original = plan_for("кафе", "парк")
                result, groups = ordered_goals(query, original)
                self.assertIs(result, original)
                self.assertEqual(groups, ())

    def test_unknown_actions_and_extra_goals_are_not_erased(self):
        for query in ("Хочу кафе, потом погладить капибару в парке",
                      "Хочу кафе, потом потренировать дракона в парке",
                      "Хочу кафе с неизвестной услугой, потом парк",
                      "Хочу кафе, потом парк и поиграть в VR",
                      "Хочу кафе, потом парк или неизвестный аттракцион"):
            with self.subTest(query=query):
                second = "парке" if "парке" in query else "парк"
                original = plan_for("кафе", second)
                result, groups = ordered_goals(query, original)
                self.assertIs(result, original)
                self.assertEqual(groups, ())

    def test_plain_or_and_conjunction_are_not_sequence_witnesses(self):
        for query in ("Хочу кафе или парк", "Хочу кафе и парк", "Кафе рядом с парком", "Потом кафе и парк"):
            original = plan_for("кафе", "парк")
            result, groups = ordered_goals(query, original)
            self.assertIs(result, original)
            self.assertEqual(groups, ())

    def test_multiple_markers_and_multiple_first_stage_goals_fail_closed(self):
        cases = (("кафе, потом парк, затем музей", plan_for("кафе", "парк", "музей")),
                 ("кафе или ресторан, потом парк", plan_for("кафе", "ресторан", "парк")),
                 ("кафе, потом парк и музей", plan_for("кафе", "парк", "музей")))
        for query, original in cases:
            with self.subTest(query=query):
                result, groups = ordered_goals(query, original)
                self.assertIs(result, original)
                self.assertEqual(groups, ())

    def test_ambiguous_overlapping_and_marker_containing_quotes_do_not_reorder(self):
        cases = (("кафе, потом кафе и парк", plan_for("кафе", "парк")),
                 ("кафе, потом парк", plan_for("кафе", "потом парк", "парк")),
                 ("кафе Потом и парк", plan_for("кафе Потом", "парк")))
        for query, original in cases:
            with self.subTest(query=query):
                result, groups = ordered_goals(query, original)
                self.assertIs(result, original)
                self.assertEqual(groups, ())

    def test_bounded_duration_and_polite_suffixes_remain_safe(self):
        for tail in (" на 60 минут", " на 1,5 часа", " for 60 minutes", " пожалуйста", " пешком", ""):
            query = "кафе, потом парк" + tail
            _, groups = ordered_goals(query, plan_for("кафе", "парк"))
            self.assertEqual(groups, ((0,), (1,)))

    def test_invalid_or_out_of_scope_inputs_preserve_original(self):
        original = plan_for("кафе")
        for query, plan in ((None, original), ("кафе потом парк", None), ("кафе потом парк", original),
                            ("кафе потом парк", plan_for("кафе", "парк", "музей", "ресторан"))):
            result, groups = ordered_goals(query, plan)
            self.assertIs(result, plan)
            self.assertEqual(groups, ())


if __name__ == "__main__":
    unittest.main()
