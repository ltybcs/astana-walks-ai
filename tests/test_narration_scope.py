"""Narration scopes are app proposals; all source/answer guards stay active."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from app.google_client import strict_json
from app.unified_narration import narrate
from tests import test_unified_narration as fixtures


class NarrationScopeTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.UnifiedNarrationTests()
        self.base.setUp()

    def single(self, category, text, *, provider="osm", name="Синтетическая точка", hours=None, rubrics=()):
        stop = deepcopy(self.base.stops[0])
        stop.update(category=category, name=name, provider=provider, opening_hours=hours,
                    source_categories=[category], source_rubrics=list(rubrics))
        if provider == "google":
            stop.update(stop_id="google:google-place-2", source_id="g2")
        else:
            stop.update(stop_id=provider + ":synthetic", source_id="d0" if provider == "dgis" else "o0")
        options = deepcopy(self.base.alternatives[:1])
        options[0]["stops"] = [stop]
        answer = {"text": text + " Условия посещения неизвестны.",
                  "stop_ids": [stop["stop_id"]], "source_ids": [stop["source_id"]],
                  "venue_descriptions": [{"stop_id": stop["stop_id"], "text": text}]}
        intent = {"categories": ["walk"] if category == "unknown" else [category], "preferences": []}
        result = self.base.run_narrator(stops=[stop], alternatives=options, intent=intent,
                                       http_open=self.base.model(answer))
        context = strict_json(self.base.calls[-1][0]["messages"][1]["content"])
        return result, context, stop, answer

    def test_twelve_scope_families_accept_free_grounded_text_without_new_facts(self):
        cases = (
            ("park", "Парк соответствует желанию прогуляться. Предлагаю прогулку после проверки условий."),
            ("cafe", "Кафе соответствует пожеланию выпить кофе. Предлагаю паузу на кофе после проверки условий."),
            ("museum", "Музей соответствует вашему интересу. Предлагаю запланировать посещение после проверки условий."),
            ("playground", "Детская площадка соответствует запросу. Предлагаю короткую остановку после проверки условий."),
            ("computer_club", "Компьютерный клуб соответствует пожеланию поиграть. Предлагаю игровую паузу после проверки условий."),
            ("pharmacy", "Аптека соответствует запросу. Предлагаю проверить условия посещения выбранной аптеки."),
            ("karaoke", "Караоке соответствует вашему интересу. Предлагаю запланировать караоке после проверки условий."),
            ("billiards", "Бильярд соответствует вашему интересу. Предлагаю запланировать бильярд после проверки условий."),
            ("nightclub", "Ночной клуб соответствует вашему интересу. Предлагаю запланировать посещение после проверки условий."),
            ("pool", "Бассейн соответствует вашему интересу. Предлагаю проверить условия его посещения."),
            ("library", "Библиотека соответствует вашему интересу. Предлагаю запланировать посещение после проверки условий."),
            ("unknown", "Выбранное место связано с прогулкой. Предлагаю короткую остановку после проверки условий."),
        )
        for category, text in cases:
            with self.subTest(category=category):
                result, context, stop, answer = self.single(category, text, hours="24/7")
                self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
                self.assertEqual(context["selected_provider_facts"][0]["opening_hours"], "24/7")
                scope = context["narration_scope"][0]
                self.assertEqual((scope["stop_id"], scope["source_id"]), (stop["stop_id"], stop["source_id"]))
                self.assertEqual(scope["own_type"]["category"], category)
                self.assertIs(scope["proposal_is_source_fact"], False)
                self.assertEqual(scope["proposal_speaker"], "assistant")
                self.assertTrue(scope["permitted_proposal"].startswith("Предложить "))
                self.assertNotIn("opening_hours", scope)
                self.assertNotIn("features", scope)
                self.assertNotIn("description", scope)
                self.assertEqual(context["grounding_lite_summary"], self.base.grounding["summary"])
                self.assertEqual(len(context["all_google_source_previews"]), 5)

    def test_broad_walk_park_has_park_proposal_not_a_forced_building_action(self):
        stop = deepcopy(self.base.stops[0])
        self.base.stops = [stop]
        self.base.alternatives = self.base.alternatives[:1]
        answer = deepcopy(self.base.answer)
        answer.update(stop_ids=[stop["stop_id"]], source_ids=[stop["source_id"]])
        result = self.base.run_narrator(intent={"categories": ["walk"], "preferences": []},
                                       http_open=self.base.model(answer))
        self.assertIsNotNone(result[0])
        context = strict_json(self.base.calls[-1][0]["messages"][1]["content"])
        scope = context["narration_scope"][0]
        self.assertEqual(scope["matching_intent_categories"], ["walk"])
        self.assertIn("парке", scope["permitted_proposal"])
        self.assertNotIn("фасад", scope["permitted_proposal"])

    def test_scope_is_own_record_only_and_never_invents_unknown_type_from_name(self):
        text = "Выбранное место связано с прогулкой. Предлагаю короткую остановку после проверки условий."
        result, context, _, _ = self.single("unknown", text, provider="google", name="Уютное кафе 24/7")
        self.assertIsNotNone(result[0])
        scope = context["narration_scope"][0]
        self.assertEqual(scope["own_type"], {"category": "unknown", "label": None, "label_origin": "unknown"})
        self.assertNotIn("кофе", scope["permitted_proposal"])
        self.assertNotIn("Уютное", str(scope))

    def test_mixed_scope_never_borrows_google_quality_hours_or_neighbour_type(self):
        base = self.base
        base.stops = base.stops[:2]
        base.stops[1].update(category="cafe", source_categories=["cafe"], opening_hours="24/7")
        base.grounding["summary"] = "Лучшее тихое кафе открыто 24/7, рядом выставка [4]. " * 8
        for option in base.alternatives:
            option["stops"] = base.stops
        answer = {"text": "Предлагаю совместить прогулку и паузу на кофе. Условия посещения неизвестны.",
                  "stop_ids": [s["stop_id"] for s in base.stops], "source_ids": ["o0", "d0"],
                  "venue_descriptions": [
                      {"stop_id": base.stops[0]["stop_id"], "text": "Парк соответствует прогулке. Предлагаю пройтись после проверки условий."},
                      {"stop_id": base.stops[1]["stop_id"], "text": "Кафе соответствует желанию выпить кофе. Предлагаю паузу на кофе после проверки условий."}]}
        original = deepcopy((base.grounding, base.stops))
        result = base.run_narrator(intent={"categories": ["park", "cafe"], "preferences": []},
                                   http_open=base.model(answer))
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
        context = strict_json(base.calls[-1][0]["messages"][1]["content"])
        park, cafe = context["narration_scope"]
        self.assertEqual(park["matching_intent_categories"], ["park"])
        self.assertEqual(cafe["matching_intent_categories"], ["cafe"])
        self.assertNotIn("кофе", park["permitted_proposal"])
        self.assertNotIn("парке", cafe["permitted_proposal"])
        self.assertNotIn("24/7", str(context["narration_scope"]))
        self.assertEqual(context["selected_provider_facts"][1]["opening_hours"], "24/7")
        self.assertEqual(context["grounding_lite_summary"], original[0]["summary"])
        self.assertEqual(len(context["all_google_source_previews"]), 5)
        self.assertEqual((base.grounding, base.stops), original)

    def test_unmapped_type_uses_only_a_unique_actual_rubric_without_promising_services(self):
        base = self.base
        base.stops = [deepcopy(base.stops[0])]
        base.stops[0].update(category="unknown", source_rubrics=["Планетарии"])
        base.alternatives = base.alternatives[:1]
        base.alternatives[0]["stops"] = base.stops
        answer = deepcopy(base.answer)
        answer.update(stop_ids=[base.stops[0]["stop_id"]], source_ids=["o0"])
        base.run_narrator(http_open=base.model(answer))
        context = strict_json(base.calls[-1][0]["messages"][1]["content"])
        scope = context["narration_scope"][0]
        self.assertEqual(scope["own_type"], {"category": "unknown", "label": "Планетарии", "label_origin": "own_source_rubric"})
        self.assertIs(scope["proposal_is_source_fact"], False)
        self.assertNotIn("экспозици", scope["permitted_proposal"])
        self.assertEqual(context["selected_provider_facts"][0]["source_rubrics"], ["Планетарии"])

    def test_numeric_quality_private_and_unselected_claims_still_reject_whole_answer(self):
        for text, expected in (
            ("Парк соответствует прогулке. Предлагаю провести здесь 275 минут.", "unsupported_number"),
            ("Парк соответствует прогулке. Это прекрасное живописное место.", "unsupported_quality_claim"),
            ("Парк соответствует прогулке. Рядом тихая зона с розетками.", "unsupported_protected_"),
            ("Предлагаю посетить Google источник 4 после проверки условий.", "unsupported_number"),
            ("Предлагаю прогулку возле PRIVATE_START_ADDRESS после проверки условий.", "sensitive_origin_in_model_output"),
        ):
            with self.subTest(text=text):
                result, _, _, _ = self.single("park", text)
                self.assertIsNone(result[0])
                self.assertIn(expected, result[1])
                self.assertEqual(len(self.base.calls), 1)
                self.base.calls.clear()

    def test_model_contract_and_full_inputs_are_unchanged_and_not_rewritten(self):
        original = deepcopy((self.base.grounding, self.base.stops, self.base.alternatives, self.base.context, self.base.intent))
        self.base.run_narrator()
        payload, url, timeout = self.base.calls[0]
        context = strict_json(payload["messages"][1]["content"])
        self.assertEqual((self.base.grounding, self.base.stops, self.base.alternatives, self.base.context, self.base.intent), original)
        self.assertEqual(context["grounding_lite_summary"], original[0]["summary"])
        self.assertEqual(context["all_google_source_previews"], [
            {key: source[key] for key in ("id", "source_id", "title", "url", "attribution")}
            for source in original[0]["sources"]])
        self.assertEqual([fact["description"] for fact in context["selected_provider_facts"]],
                         [stop["description"] for stop in original[1]])
        self.assertEqual([scope["stop_id"] for scope in context["narration_scope"]],
                         [stop["stop_id"] for stop in original[1]])
        self.assertEqual(set(payload["response_format"]["json_schema"]["schema"]["properties"]),
                         {"text", "source_ids", "stop_ids", "venue_descriptions"})
        self.assertEqual((payload["max_tokens"], payload["temperature"], payload["cache_prompt"], timeout), (950, 0, False, 120))
        self.assertEqual(url, "http://127.0.0.1:8081/v1/chat/completions")

    def test_observed_temporal_suitability_phrases_reject_both_whole_text_and_description(self):
        for phrase in (
            "Парк подходит для прогулки в ночное время.",
            "Предлагаю посещение парка в течение дня.",
            "Идея: вечерняя прогулка по выбранному парку.",
            "Парк подходит для посещения днем.",
            "Идея: прогуляться вечером после проверки условий.",
            "Можно зайти в любое время.",
        ):
            with self.subTest(phrase=phrase):
                result, context, _, _ = self.single("park", phrase, hours="24/7")
                self.assertEqual(result, (None, "unsupported_temporal_claim"))
                self.assertNotIn("time_of_day", context)
                self.assertEqual(context["selected_provider_facts"][0]["opening_hours"], "24/7")
                self.assertEqual(context["grounding_lite_summary"], self.base.grounding["summary"])
        result, _, stop, answer = self.single("park", "Парк соответствует прогулке. Предлагаю пройтись после проверки условий.")
        answer["venue_descriptions"][0]["text"] = "Парк подходит для прогулки в ночное время после проверки условий."
        self.base.calls.clear()
        options = deepcopy(self.base.alternatives[:1]); options[0]["stops"] = [stop]
        result = self.base.run_narrator(stops=[stop], alternatives=options, intent={"categories": ["park"], "preferences": []},
                                       http_open=self.base.model(answer))
        self.assertEqual(result, (None, "venue_description_unsupported_temporal_claim"))

    def test_temporal_names_and_nightclub_type_keep_their_identity_exemption(self):
        for category, name, phrase in (
            ("park", "Вечерний парк", "Вечерний парк соответствует прогулке. Предлагаю пройтись после проверки условий."),
            ("park", "В ночное время", "В ночное время соответствует запросу. Предлагаю короткую остановку после проверки условий."),
            ("nightclub", "Синтетический клуб", "Ночной клуб соответствует интересу. Предлагаю запланировать посещение после проверки условий."),
        ):
            with self.subTest(category=category, name=name):
                result, _, _, answer = self.single(category, phrase, name=name)
                self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))

    def test_generic_museum_cannot_invent_historical_space_or_cultural_status(self):
        for phrase in (
            "Идея — прогулка в историческом пространстве, связанном с культурным наследием.",
            "Предлагаю посещение исторического музея после проверки условий.",
            "Музей расположен в историческом здании. Предлагаю запланировать посещение.",
        ):
            with self.subTest(phrase=phrase):
                result, _, _, _ = self.single("museum", phrase)
                self.assertEqual(result, (None, "unsupported_venue_subtype"))
        for phrase in (
            "Музей находится в историческом здании. Предлагаю запланировать посещение.",
            "Это памятник культурного наследия. Предлагаю запланировать посещение музея.",
        ):
            with self.subTest(proven_theme_only=phrase):
                result, _, _, _ = self.single("museum", phrase, rubrics=["Исторические музеи"])
                self.assertEqual(result, (None, "unsupported_venue_subtype"))
        result, _, _, answer = self.single("museum", "Исторический музей соответствует интересу. Предлагаю запланировать посещение.",
                                           rubrics=["Исторические музеи"])
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
        result, _, _, answer = self.single("museum", "Исторический музей соответствует интересу. Предлагаю запланировать посещение.",
                                           name="Исторический музей")
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))

    def test_promenade_seed_is_a_positive_assistant_idea_without_catalog_safety_caveat(self):
        result, context, _, answer = self.single("promenade", "Набережная соответствует прогулке. Предлагаю прогуляться по набережной.")
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
        scope = context["narration_scope"][0]
        self.assertEqual(scope["permitted_proposal"], "Предложить прогулку по набережной.")
        self.assertEqual(scope["proposal_speaker"], "assistant")
        self.assertIs(scope["proposal_is_source_fact"], False)
        self.assertNotIn("безопасност", str(scope))
        result, _, _, _ = self.single("promenade", "Набережная предлагает прогулку без гарантий безопасности.")
        self.assertEqual(result, (None, "unsupported_protected_access_safety"))

    def test_whole_text_cannot_borrow_one_museums_theme_for_another(self):
        base = self.base
        base.stops = base.stops[:2]
        for stop in base.stops:
            stop.update(category="museum", source_categories=["museum"])
        base.stops[0]["source_rubrics"] = ["Исторические музеи"]
        for option in base.alternatives:
            option["stops"] = base.stops
        answer = {"text": "Исторический музей соответствует интересу. Предлагаю запланировать посещение.",
                  "stop_ids": [s["stop_id"] for s in base.stops], "source_ids": ["o0", "d0"],
                  "venue_descriptions": [
                      {"stop_id": base.stops[0]["stop_id"], "text": "Исторический музей соответствует интересу. Предлагаю запланировать посещение."},
                      {"stop_id": base.stops[1]["stop_id"], "text": "Музей соответствует интересу. Предлагаю запланировать посещение."}]}
        result = base.run_narrator(intent={"categories": ["museum"], "preferences": []}, http_open=base.model(answer))
        self.assertEqual(result, (None, "unsupported_venue_subtype"))
        answer["text"] = "Музеи соответствуют вашему интересу. Предлагаю запланировать посещение после проверки условий."
        result = base.run_narrator(intent={"categories": ["museum"], "preferences": []}, http_open=base.model(answer))
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
        answer["venue_descriptions"][1]["text"] = "Исторический музей соответствует интересу. Предлагаю запланировать посещение."
        result = base.run_narrator(intent={"categories": ["museum"], "preferences": []}, http_open=base.model(answer))
        self.assertEqual(result, (None, "venue_description_unsupported_venue_subtype"))

    def test_observed_park_gaming_and_facade_inventions_are_rejected(self):
        for phrase in (
            "Парк предлагает игровую паузу после проверки условий.",
            "Предлагаю посмотреть здание с характерным фасадом во время прогулки.",
            "Кафе — удобное место для перерыва. Предлагаю паузу на кофе.",
        ):
            result, _, _, _ = self.single("cafe" if "Кафе" in phrase else "park", phrase)
            self.assertIsNone(result[0])
        result, _, _, answer = self.single("playground", "Предлагаю игровую паузу на детской площадке после проверки условий.")
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))
        result, _, _, answer = self.single("park", "Удобный парк соответствует прогулке. Предлагаю короткую остановку.", name="Удобный парк")
        self.assertEqual(result, (answer, "validated_structure_sources_and_known_claims"))

    def test_bad_route_still_blocks_before_model_even_with_a_valid_scope_type(self):
        alternatives = deepcopy(self.base.alternatives)
        for option in alternatives:
            option["route_built"] = False
        opened = self.base.model()
        result = self.base.run_narrator(alternatives=alternatives, http_open=opened)
        self.assertEqual(result, (None, "route_not_budget_verified"))
        self.assertEqual(self.base.calls, [])


if __name__ == "__main__":
    unittest.main()
