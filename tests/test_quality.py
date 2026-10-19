"""Quality regressions, not a claim of live LLM or field-verified city quality."""

from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from app.generation import _finish_sentence, recommend
from app.llm import LLMConfig, narrate, russian_context, validate_narration, validate_narration_with_reason
from app.retrieval import Retriever, detect_intent
from tests.test_retrieval import ORIGIN, index, poi


class RetrievalQualityTests(unittest.TestCase):
    def test_negations_do_not_turn_into_positive_requirements(self):
        intent = detect_intent("Кофе без розетки, без детей, не хочу музей")
        self.assertTrue(intent["coffee"])
        self.assertFalse(intent["socket"])
        self.assertFalse(intent["child"])
        self.assertIn("museum", intent["excluded_categories"])
        self.assertFalse(detect_intent("Кофе, розетка не нужна")["socket"])
        self.assertFalse(detect_intent("Не обязательно тихое кафе")["strict_quiet"])
        self.assertFalse(detect_intent("Семь минут прогулки")["child"])

    def test_unrelated_question_does_not_become_an_arbitrary_place(self):
        response = recommend(index(poi("p")), "Сколько будет два плюс два", 60, ORIGIN, use_llm=False)
        self.assertEqual(response["recommendations"], [])
        self.assertIn("не совпал", response["clarification"])

    def test_multiple_interests_can_combine_categories(self):
        response = recommend(index(poi("park"), poi("coffee", category="cafe"), poi("mall", category="mall")), "Кофе и парк", 100, ORIGIN, use_llm=False)
        self.assertEqual({item["category"] for item in response["recommendations"]}, {"park", "cafe"})

    def test_explicit_park_and_architecture_can_both_be_in_the_plan(self):
        response = recommend(index(poi("park"), poi("memorial", category="monument")), "Парк и архитектура", 100, ORIGIN, use_llm=False)
        self.assertEqual({item["category"] for item in response["recommendations"]}, {"park", "monument"})

    def test_plain_walk_does_not_fill_itinerary_with_malls_or_cafes(self):
        response = recommend(index(poi("mall", category="mall"), poi("coffee", category="cafe"), poi("park")), "Прогулка возле Mega", 100, ORIGIN, use_llm=False)
        self.assertEqual([item["id"] for item in response["recommendations"]], ["park"])

    def test_same_place_node_and_way_are_not_two_stops(self):
        first, second = poi("node"), poi("way", lat=51.09001)
        first["name"] = second["name"] = "Городской парк"
        response = recommend(index(first, second), "парк", 120, ORIGIN, use_llm=False)
        self.assertEqual(len(response["recommendations"]), 1)

    def test_paid_fields_cannot_buy_a_better_position(self):
        first, second = poi("a"), poi("b")
        retriever = index(first, second)
        before = retriever.retrieve("парк", ORIGIN, 90)
        second.update({"sponsored": True, "paid_boost": 1_000_000, "rating": 5})
        after = index(first, second).retrieve("парк", ORIGIN, 90)
        self.assertEqual([item["poi"]["id"] for item in before["selected"]], [item["poi"]["id"] for item in after["selected"]])

    def test_kazakh_child_walk_context_is_supported(self):
        intent = detect_intent("Балалармен саябақта серуен")
        self.assertTrue(intent["child"])
        self.assertTrue(intent["outdoor"])
        self.assertIn("park", intent["preferred_categories"])

    def test_strict_quiet_request_abstains_without_source_evidence(self):
        response = recommend(index(poi("cafe", category="cafe")), "Только тихое кафе", 60, ORIGIN, use_llm=False)
        self.assertEqual(response["recommendations"], [])
        self.assertIn("тишиной", response["clarification"])

    def test_private_access_is_not_recommended(self):
        private = poi("private")
        private["source_tags"] = {"access": "private"}
        self.assertEqual(recommend(index(private), "парк", 60, ORIGIN, use_llm=False)["recommendations"], [])

    def test_dataset_validation_rejects_corruption_and_unsafe_sources(self):
        bad = [poi("a", lat=float("nan")), poi("a", lat=10 ** 500), poi("a", visit=-5)]
        unsafe = poi("unsafe")
        unsafe["source_url"] = "javascript:alert(1)"
        bad.append(unsafe)
        for place in bad:
            with self.subTest(place=place["id"]), self.assertRaises(ValueError):
                index(place)
        with self.assertRaises(ValueError):
            index(poi("same"), poi("same"))


class NarrationQualityTests(unittest.TestCase):
    def setUp(self):
        self.facts = {"p": {"name": "Парк", "category": "park", "description": "По источнику — парк.", "features": {"quiet": "unknown", "socket": "unknown", "wheelchair": "unknown"}, "opening_hours": None, "walk_minutes": 5, "visit_minutes": 20, "distance_m": 300}}
        self.good = {"recommendations": [{"id": "p", "why": "Парк подходит для прогулки на свежем воздухе.", "action": "Идея: пройтись по парку после проверки доступа.", "citations": ["p"], "fact_keys": ["category"]}]}

    def test_normal_free_russian_text_is_accepted(self):
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    def test_observed_family_park_cannot_invent_a_playground(self):
        self.facts["p"]["name"] = "Парк Астана"
        self.good["recommendations"][0]["why"] = "Парк Астана соответствует категории 'парк или сад' и находится в день недели, подходит для прогулки с ребёнком."
        self.good["recommendations"][0]["action"] = "Идея: осмотреть зоны с детской площадкой и зелёной зоной для отдыха."
        self.assertEqual(validate_narration_with_reason(self.good, self.facts)[1], "unsupported_playground_claim")

    def test_playground_requires_source_category_or_description_and_names_are_literal(self):
        self.good["recommendations"][0]["action"] = "Идея: осмотреть детскую площадку после проверки доступа."
        self.facts["p"]["category"] = "playground"
        self.assertIsNotNone(validate_narration(self.good, self.facts))
        self.facts["p"]["category"] = "park"
        self.facts["p"]["description"] = "Источник прямо указывает детскую площадку на территории."
        self.assertIsNotNone(validate_narration(self.good, self.facts))
        self.facts["p"]["description"] = "Детская площадка не подтверждена."
        self.assertIsNone(validate_narration(self.good, self.facts))
        self.good["recommendations"][0]["action"] = "Идея: уточните наличие детской площадки перед выходом."
        self.assertIsNotNone(validate_narration(self.good, self.facts))
        self.facts["p"]["name"] = "Детская площадка"
        self.good["recommendations"][0]["action"] = "Идея: осмотреть место «Детская площадка» после проверки доступа."
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    def test_interactive_equipment_is_not_implied_by_park_or_playground(self):
        self.good["recommendations"][0]["action"] = "Идея: осмотреть интерактивные экспонаты во время прогулки."
        self.assertEqual(validate_narration_with_reason(self.good, self.facts)[1], "unsupported_interactive_equipment_claim")
        self.facts["p"]["description"] = "Источник указывает интерактивные экспонаты."
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    @patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake"}, clear=True)
    def test_observed_playground_location_is_rejected_and_repair_is_diagnosed(self):
        self.facts["p"].update({"name": "Без названия: детская площадка (OSM way/1131138292)", "category": "playground", "description": "По источнику — детская площадка."})
        unsafe = deepcopy(self.good)
        unsafe["recommendations"][0].update({"why": "Детская площадка соответствует интересу в прогулке с ребёнком и наличии условий для детей", "action": "Идея: посмотреть на детскую площадку в парке"})
        self.assertEqual(validate_narration_with_reason(unsafe, self.facts)[1], "unsupported_location_claim")
        repaired = deepcopy(unsafe)
        repaired["recommendations"][0]["action"] = "Идея: осмотреть детскую площадку после проверки доступа."
        responses = [io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}).encode()) for value in (unsafe, repaired)]
        diagnostics = {}
        with patch("app.llm.open_url", side_effect=responses):
            result, status = narrate([{"id": "p"}], {}, facts_by_id=self.facts, diagnostics=diagnostics)
        self.assertIsNotNone(result)
        self.assertEqual(status, "validated_after_repair")
        self.assertEqual(diagnostics, {"attempts": 2, "rejections": ["unsupported_location_claim"]})

    def test_park_location_requires_own_source_evidence_or_park_category(self):
        self.facts["p"].update({"name": "Площадка", "category": "playground", "description": "По источнику — детская площадка."})
        for location in ("в парке", "в саду"):
            self.good["recommendations"][0]["action"] = "Идея: осмотреть площадку " + location + "."
            with self.subTest(location=location):
                self.assertEqual(validate_narration_with_reason(self.good, self.facts)[1], "unsupported_location_claim")
                self.facts["p"]["description"] = "Источник: площадка находится " + location + "."
                self.assertIsNotNone(validate_narration(self.good, self.facts))
                self.facts["p"]["description"] = "Расположение " + location + " не подтверждено."
                self.assertIsNone(validate_narration(self.good, self.facts))
                self.facts["p"]["category"] = "park"
                self.assertIsNotNone(validate_narration(self.good, self.facts))
                self.facts["p"].update({"category": "playground", "description": "По источнику — детская площадка."})

    def test_literal_source_name_with_park_location_is_preserved(self):
        self.facts["p"].update({"name": "Кафе в парке", "category": "cafe", "description": "По источнику — кафе."})
        self.good["recommendations"][0]["action"] = "Идея: осмотреть «Кафе в парке» после проверки доступа."
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    def test_deterministic_access_reminder_starts_a_new_sentence(self):
        self.assertEqual(_finish_sentence("Идея: наблюдать за природой"), "Идея: наблюдать за природой.")
        self.assertEqual(_finish_sentence("Идея: осмотреть парк."), "Идея: осмотреть парк.")
        self.assertEqual(_finish_sentence("Идея: осмотреть парк!"), "Идея: осмотреть парк!")

    def test_context_translates_machine_labels_without_copying_route_numbers(self):
        context = russian_context(self.facts, {"outdoor": True, "time_of_day": "day"})
        self.assertEqual(context["retrieved_poi_facts"]["p"]["category"], "парк или сад")
        self.assertEqual(context["retrieved_poi_facts"]["p"]["features"]["тишина"], "неизвестно")
        self.assertNotIn("walk_minutes", context["retrieved_poi_facts"]["p"])

    def test_observed_qwen_numeric_fields_cannot_be_reinterpreted_as_bike_time(self):
        self.facts["p"].update({"name": "NU Park", "distance_m": 775, "walk_minutes": 11, "visit_minutes": 20})
        self.good["recommendations"][0]["why"] = "Парк NU Park находится в 775 метрах от вас, с возможностью посетить в 11 минут на foot и 20 минут на bike."
        self.assertIsNone(validate_narration(self.good, self.facts))

    def test_route_numbers_and_nonwalking_transport_are_rejected_even_when_values_exist(self):
        for text in ("Парк расположен в 300 метрах от вас.", "До парка пять минут пешком.", "Можно доехать на велосипеде.", "Идея: приехать на машине и осмотреть парк.", "Идея: взять самокат для прогулки.", "Можно пойти пешком или вызвать такси.", "Идея: немного погулять на bike."):
            payload = deepcopy(self.good)
            payload["recommendations"][0]["why"] = text
            with self.subTest(text=text):
                self.assertIsNone(validate_narration(payload, self.facts))

    def test_literal_source_name_can_contain_latin_letters_and_digits(self):
        self.facts["p"]["name"] = "NU Park 20"
        self.good["recommendations"][0]["why"] = "NU Park 20 подходит для короткой прогулки на свежем воздухе."
        self.assertIsNotNone(validate_narration(self.good, self.facts))
        self.good["recommendations"][0]["action"] = "Идея: осмотреть NU Park 20 после проверки доступа и часов работы."
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    def test_hallucinated_protected_facts_fall_back(self):
        for text in ("Здесь всегда тихо и уютно.", "Здесь есть розетки для зарядки.", "Парк открыт сейчас и работает.", "Вход стоит 500 тенге для всех.", "Рейтинг парка — самый высокий.", "Доступно для колясок без ограничений.", "Здесь безопасно ночью для детей.", "Дойти можно за 99 минут.", "Смотрите https://invented.example/location", "В парке сегодня проходит концерт.", "Есть туалеты и бесплатный Wi-Fi."):
            value = deepcopy(self.good)
            value["recommendations"][0]["why"] = text
            with self.subTest(text=text):
                self.assertIsNone(validate_narration(value, self.facts))

    def test_uncertainty_is_accepted_without_claiming_quietness(self):
        self.good["recommendations"][0]["why"] = "Парк подходит для прогулки; тишина не подтверждена источником."
        self.assertIsNotNone(validate_narration(self.good, self.facts))

    def test_observed_unsupported_surface_and_location_have_specific_reasons(self):
        for text, reason in (("Парк подходит для отдыха с плавными дорожками.", "unsupported_surface_claim"), ("Памятник находится в центре Астаны.", "unsupported_location_claim"), ("Парк обеспечивает доступ для пешеходов.", "unsupported_access_claim"), ("Идея: осмотреть зоны с видом на город и отдыхать в зеленых зонах.", "unsupported_view_claim")):
            payload = deepcopy(self.good)
            payload["recommendations"][0]["why"] = text
            with self.subTest(text=text):
                self.assertEqual(validate_narration_with_reason(payload, self.facts)[1], reason)

    @patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake", "LLM_TIMEOUT_SECONDS": "120"}, clear=True)
    def test_one_repair_keeps_guard_and_shares_the_total_deadline(self):
        unsafe = deepcopy(self.good)
        unsafe["recommendations"][0]["why"] = "Парк позволяет насладиться тишиной и спокойствием."
        clock = [0.0]
        bodies = []
        timeouts = []
        def provider(request, timeout):
            bodies.append(json.loads(request.data))
            timeouts.append(timeout)
            content = unsafe if len(bodies) == 1 else self.good
            clock[0] = 80.0 if len(bodies) == 1 else 90.0
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}).encode())
        diagnostics = {}
        with patch("app.llm.monotonic", side_effect=lambda: clock[0]), patch("app.llm.open_url", side_effect=provider):
            result, status = narrate([{"id": "p"}], {}, facts_by_id=self.facts, diagnostics=diagnostics)
        self.assertIsNotNone(result)
        self.assertEqual(status, "validated_after_repair")
        self.assertEqual(diagnostics, {"attempts": 2, "rejections": ["unsupported_quiet_claim"]})
        self.assertEqual(timeouts, [120, 40])
        self.assertIn("unsupported_quiet_claim", bodies[1]["messages"][-1]["content"])
        self.assertFalse(any(message["role"] == "assistant" for message in bodies[1]["messages"]))

    @patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake"}, clear=True)
    def test_two_invalid_responses_fall_back_without_extra_attempts(self):
        unsafe = deepcopy(self.good)
        unsafe["recommendations"][0]["why"] = "Можно доехать на велосипеде до парка."
        envelope = json.dumps({"choices": [{"message": {"content": json.dumps(unsafe)}}]}).encode()
        diagnostics = {}
        with patch("app.llm.open_url", side_effect=lambda *_: io.BytesIO(envelope)) as mock:
            result, status = narrate([{"id": "p"}], {}, facts_by_id=self.facts, diagnostics=diagnostics)
        self.assertIsNone(result)
        self.assertEqual(status, "validation_failed")
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(diagnostics["rejections"], ["unsupported_transport", "unsupported_transport"])

    def test_wrong_ids_citations_and_fact_keys_are_rejected(self):
        for key, value in (("id", "other"), ("citations", ["other"]), ("fact_keys", ["invented"]), ("walk_minutes", -1)):
            payload = deepcopy(self.good)
            payload["recommendations"][0][key] = value
            with self.subTest(key=key):
                self.assertIsNone(validate_narration(payload, self.facts))

    @patch.dict("os.environ", {"LLM_MODEL": "local-model", "LLM_BASE_URL": "http://127.0.0.1:11434/v1"}, clear=True)
    def test_loopback_provider_can_work_without_an_api_key(self):
        self.assertIsNotNone(LLMConfig.from_env())

    def test_remote_cleartext_and_invalid_config_are_disabled(self):
        for base in ("http://remote.example/v1", "https://key:secret@example.com/v1", "http://["):
            with self.subTest(base=base), patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake", "LLM_BASE_URL": base}, clear=True):
                self.assertIsNone(LLMConfig.from_env())

    @patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake"}, clear=True)
    def test_provider_receives_retrieved_facts_without_user_query_or_origin(self):
        payload = {"recommendations": [{"id": "p", "why": "Парк подходит для прогулки на свежем воздухе.", "action": "Идея: пройтись по парку после проверки доступа.", "citations": ["p"], "fact_keys": ["category"]}]}
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]}
        with patch("app.llm.open_url", return_value=io.BytesIO(json.dumps(envelope).encode())) as mock:
            response = recommend(index(poi("p")), "query-secret 987654 прогулка", 60, {**ORIGIN, "label": "label-secret"})
        body = json.loads(mock.call_args.args[0].data)
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("query-secret", serialized)
        self.assertNotIn("label-secret", serialized)
        self.assertNotIn(str(ORIGIN["lat"]), serialized)
        context = json.loads(body["messages"][1]["content"])["retrieved_poi_facts"]["p"]
        self.assertNotIn("walk_minutes", context)
        self.assertNotIn("visit_minutes", context)
        self.assertNotIn("distance_m", context)
        self.assertNotIn("opening_hours", context)
        self.assertEqual(response["mode"], "llm-grounded")

    @patch.dict("os.environ", {"LLM_MODEL": "test", "LLM_API_KEY": "fake"}, clear=True)
    def test_network_failure_keeps_useful_recommendations(self):
        with patch("app.llm.open_url", side_effect=TimeoutError("not exposed")):
            response = recommend(index(poi("p")), "прогулка", 60, ORIGIN)
        self.assertTrue(response["recommendations"])
        self.assertEqual(response["mode"], "extractive")
        self.assertEqual(response["diagnostics"]["llm_status"], "provider_unavailable")
        self.assertNotIn("not exposed", json.dumps(response))
