"""Category consumers stay consistent; all model/provider material is synthetic."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.category_catalog import CATEGORY_CATALOG, CATEGORY_IDS
from app.google_client import json_bytes, strict_json
from app.google_grounded import prepare, safe_intent
from app.google_budget import GoogleError
from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.llm import LLMConfig
from app.retrieval import Retriever, detect_intent
from app.unified_narration import narrate


REQUESTS = {
    "walk": "Хочу погулять", "park": "Хочу в парк", "promenade": "Хочу на набережную",
    "playground": "Хочу на детскую площадку", "cafe": "Хочу кофе", "restaurant": "Хочу в ресторан",
    "bar": "Хочу в бар", "museum": "Хочу в музей", "theatre": "Хочу в театр",
    "architecture": "Хочу посмотреть архитектуру", "mall": "Хочу в торговый центр",
    "computer_club": "Хочу в комп клуб", "cinema": "Хочу в кинотеатр", "bowling": "Хочу в боулинг",
    "billiards": "Хочу сыграть в бильярд", "karaoke": "Хочу в караоке", "escape_room": "Хочу в квест-комнату",
    "board_games": "Хочу настольные игры", "anti_cafe": "Хочу в антикафе", "trampoline": "Хочу на батуты",
    "climbing": "Хочу на скалодром", "ice_rink": "Хочу на каток", "pool": "Хочу поплавать в бассейне",
    "spa": "Хочу в СПА", "amusement_park": "Хочу в парк аттракционов", "fitness": "Хочу в фитнес-клуб",
    "bookshop": "Хочу в книжный магазин", "library": "Хочу в библиотеку", "coworking": "Хочу в коворкинг",
    "nightclub": "Хочу в ночной клуб",
}
ORIGIN = PUBLIC_ORIGINS["baiterek"]


def place(identifier, category, *, close=False):
    return {"id": identifier, "name": "Синтетическая башня" if category == "attraction" else "Синтетическое место",
            "lat": ORIGIN["lat"] + (.0001 if close else .001), "lon": ORIGIN["lon"] + .001,
            "category": category, "description": "Категория из синтетического источника.",
            "features": {}, "opening_hours": "24/7", "source_url": "https://www.openstreetmap.org/node/1"}


def retrieve(query, places, *, period="day"):
    return Retriever(data={"dataset": {"is_demo": True}, "places": deepcopy(places)}).retrieve(query, ORIGIN, 120, period)


class Response:
    def __init__(self, value):
        self.raw = json_bytes(value)
    def read(self, size=-1):
        return self.raw[:size] if size >= 0 else self.raw
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False


class CategorySearchTests(unittest.TestCase):
    def test_all_thirty_categories_reach_google_as_static_primary_search_without_raw_text(self):
        self.assertEqual(set(REQUESTS), CATEGORY_IDS)
        for category, phrase in REQUESTS.items():
            with self.subTest(category=category):
                query = phrase + " рядом с Байтереком PRIVATE_RAW_MESSAGE"
                intent = detect_intent(query)
                self.assertEqual(intent["primary_category"], category)
                self.assertIn(category, intent["preferred_categories"])
                context, safe, text, _, parameters = prepare({"query": query, "minutes": 120, "time_of_day": "day"}, resolve_request)
                self.assertIn(category, safe["categories"])
                self.assertIn(CATEGORY_CATALOG[category]["google_query"], text)
                if CATEGORY_CATALOG[category]["mode"] == "single_venue":
                    self.assertEqual(safe["categories"], [category])
                self.assertNotIn(query, json.dumps(parameters, ensure_ascii=False))
                self.assertNotIn("PRIVATE_RAW_MESSAGE", json.dumps(parameters, ensure_ascii=False))
                self.assertEqual(parameters["pageSize"], 5)
                self.assertIs(parameters["includeExtendedDetails"], False)
                self.assertEqual(parameters["locationBias"]["circle"]["center"],
                                 {"latitude": context["origin"]["lat"], "longitude": context["origin"]["lon"]})

    def test_real_retrieval_filters_wrong_source_types_and_empty_results_for_every_category(self):
        for category, phrase in REQUESTS.items():
            with self.subTest(category=category):
                source = CATEGORY_CATALOG[category]["source_categories"][0]
                matching = place("osm:node/1", source)
                unrelated_category = "cafe" if "park" in CATEGORY_CATALOG[category]["source_categories"] else "park"
                unrelated = place("osm:node/2", unrelated_category, close=True)
                query = phrase + " рядом с Байтереком"
                result = retrieve(query, [unrelated, matching])
                self.assertEqual([item["poi"]["id"] for item in result["selected"]], [matching["id"]])
                self.assertEqual(retrieve(query, [unrelated])["selected"], [])

    def test_secondary_coffee_or_architecture_does_not_replace_primary_requested_venue(self):
        for category in ("bowling", "cinema", "fitness", "library", "computer_club"):
            with self.subTest(category=category):
                query = REQUESTS[category] + " и кофе, интересует архитектура рядом с Байтереком"
                self.assertEqual(detect_intent(query)["preferred_categories"], [category])
                self.assertEqual(safe_intent(query)["categories"], [category])
                primary = place("osm:node/1", CATEGORY_CATALOG[category]["source_categories"][0])
                other = [place("osm:node/2", "cafe", close=True), place("osm:node/3", "museum", close=True)]
                self.assertEqual(retrieve(query, other)["selected"], [])
                self.assertEqual([item["poi"]["id"] for item in retrieve(query, [primary] + other)["selected"]], [primary["id"]])
        for query in ("Хочу в обсерваторию и кофе рядом с Байтереком", "find a planetarium and coffee near Baiterek"):
            with self.subTest(unsupported=query), self.assertRaises(GoogleError) as raised:
                safe_intent(query)
            self.assertEqual(raised.exception.code, "unsupported_activity")

    def test_exclusions_and_child_adult_place_prohibition_survive_category_projection(self):
        query = "Не хочу в кинотеатр, хочу кофе рядом с Байтереком"
        intent = detect_intent(query)
        self.assertEqual(intent["preferred_categories"], ["cafe"])
        self.assertIn("cinema", intent["excluded_categories"])
        self.assertEqual([item["poi"]["category"] for item in retrieve(query, [place("osm:node/1", "cinema"), place("osm:node/2", "cafe")])["selected"]], ["cafe"])
        for category in ("bar", "nightclub"):
            with self.subTest(category=category):
                self.assertEqual(retrieve(REQUESTS[category] + " с ребёнком рядом с Байтереком", [place("osm:node/1", category)])["selected"], [])

    def test_architecture_still_requires_evidence_and_can_combine_with_park_in_walk_mode(self):
        query = "Хочу архитектуру рядом с Байтереком"
        wrong = place("osm:node/1", "attraction")
        wrong["name"] = "Синтетическое место без архитектурных фактов"
        self.assertEqual(retrieve(query, [wrong])["selected"], [])
        proven = {**wrong, "source_tags": {"historic": "building"}}
        self.assertEqual(len(retrieve(query, [proven])["selected"]), 1)
        query = "Хочу в парк, интересует архитектура рядом с Байтереком"
        park = {**place("osm:node/1", "park"), "name": "Синтетический парк"}
        monument = {**place("osm:node/2", "monument"), "name": "Синтетический памятник"}
        self.assertEqual(detect_intent(query)["activity_mode"], "walk")
        self.assertEqual({item["poi"]["category"] for item in retrieve(query, [park, monument])["selected"]},
                         {"park", "monument"})
        self.assertEqual(retrieve(query, [wrong])["selected"], [])

    def test_new_venue_categories_keep_source_only_socket_quiet_access_and_night_filters(self):
        base = place("osm:node/1", "bowling")
        cases = (("Хочу боулинг и розетку", "socket"), ("Хочу только тихий боулинг", "quiet"),
                 ("Хочу боулинг с доступом для коляски", "wheelchair"))
        for query, feature in cases:
            with self.subTest(feature=feature):
                self.assertEqual(retrieve(query, [base])["selected"], [])
                proven = {**base, "features": {feature: "yes"}}
                self.assertEqual(len(retrieve(query, [proven])["selected"]), 1)
        club = {**place("osm:node/2", "nightclub"), "opening_hours": None}
        for query in ("Хочу в ночной клуб", "nightclub near Baiterek"):
            self.assertFalse(detect_intent(query, "day")["night"])
            self.assertEqual(len(retrieve(query, [club])["selected"]), 1)
            self.assertEqual(retrieve(query, [club], period="night")["selected"], [])
        self.assertTrue(detect_intent("Хочу в ночной клуб ночью", "day")["night"])
        self.assertEqual(retrieve("Хочу в ночной клуб ночью", [club])["selected"], [])

    def test_mandatory_natural_scenarios_preserve_goal_and_never_invent_requested_properties(self):
        examples = [
            ("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут — что посоветуете?", "walk"),
            ("Хочу тихое место с кофе и розеткой недалеко от байтерека.", "cafe"),
            ("Еду с ребёнком в центральный парк — где рядом есть детские площадки или музей с интерактивами?", "family_or"),
            ("Я - турист около аэропорта, у меня 2 часа перед вылетом: что можно посмотреть поблизости?", "tourist"),
            ("Ищу необычную ночную активность рядом с абу даби.", "night"),
            ("Я у здании Экспо 2017, хочу что-то необычное на 1–2 часа, пешком.", "walk"),
        ]
        for query, scenario in examples:
            with self.subTest(scenario=scenario):
                detected, safe = detect_intent(query), safe_intent(query)
                self.assertFalse(detected["unsupported_activity"])
                self.assertTrue(safe["categories"])
                if scenario == "cafe":
                    self.assertEqual(safe["categories"], ["cafe"])
                    self.assertTrue(detected["quiet"] and detected["socket"])
                elif scenario == "family_or":
                    self.assertTrue(detected["child"])
                    self.assertEqual(set(detected["preferred_categories"]), {"playground", "museum"})
                    self.assertEqual(detected["activity_mode"], "walk")
                    for category in ("playground", "museum"):
                        item = place("osm:node/1", category)
                        self.assertEqual([value["poi"]["category"] for value in retrieve(query, [item])["selected"]], [category])
                    self.assertNotIn("interactive", detected["search_intent"]["preferences"])
                elif scenario == "tourist":
                    self.assertTrue(detected["tourist"])
                    self.assertEqual(safe["categories"], ["architecture"])
                elif scenario == "night":
                    self.assertTrue(detected["night"])
                    candidate = {**place("osm:node/1", "park"), "opening_hours": None}
                    self.assertEqual(retrieve(query, [candidate])["selected"], [])
                else:
                    self.assertIn("walk", safe["categories"])

    def test_requested_interactives_events_or_rooftop_are_not_confirmed_by_a_category(self):
        config = LLMConfig("http://127.0.0.1:8081/v1", "mock-local", "", 120, "schema", "max_tokens")
        grounding = {"summary": "Полный исходный Google контекст.", "sources": []}
        stop = {**place("osm:node/1", "museum"), "provider": "osm", "source_id": "o0",
                "stop_id": "osm:node/1", "name": "Интерактивная выставка"}
        option = {"id": "budget-120", "budget_minutes": 120, "route_built": True, "budget_status": "fits",
                  "plan": {"budget_minutes": 120, "total_minutes": 120, "walk_minutes": 20,
                           "visit_minutes": 95, "buffer_minutes": 5, "return_walk_minutes": 10,
                           "verified": True, "returns_to_origin": True}, "stops": [stop]}
        cases = (("В музее есть интерактивные экспозиции.", True),
                 ("Здесь проходит выставка.", True), ("Предлагаю выйти на крышу.", True),
                 ("Предлагаю посмотреть экспозицию.", True),
                 ("Уточните наличие интерактивных экспозиций.", False),
                 ("Выставки не подтверждены.", False), ("Уточните условия посещения крыши.", False),
                 ("Предлагаю запланировать посещение места Интерактивная выставка.", False))
        for description, rejected in cases:
            with self.subTest(description=description):
                value = {"text": description + " Условия посещения неизвестны.",
                         "source_ids": ["o0"], "stop_ids": ["osm:node/1"]}
                calls = []
                def opened(request, timeout):
                    calls.append(request)
                    return Response({"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})
                with patch("app.unified_narration.open_local_model", side_effect=AssertionError("No real model HTTP")):
                    answer, status = narrate(grounding, [stop], [option], {"time_of_day": "day"},
                                             {"categories": ["museum"], "preferences": []}, config, http_open=opened)
                self.assertEqual(len(calls), 1)
                if rejected:
                    self.assertIsNone(answer)
                    self.assertEqual(status, "unsupported_protected_event_or_roof")
                else:
                    self.assertEqual(answer, value, status)

    def test_single_venue_action_cannot_be_replaced_by_a_park_or_facade(self):
        config = LLMConfig("http://127.0.0.1:8081/v1", "mock-local", "", 120, "schema", "max_tokens")
        grounding = {"summary": "Полный исходный Google контекст.", "sources": []}
        for category, spec in CATEGORY_CATALOG.items():
            if spec["mode"] != "single_venue" or category in {"cafe", "computer_club"}:
                continue
            stop = {**place("osm:node/1", spec["source_categories"][0]), "provider": "osm",
                    "source_id": "o0", "stop_id": "osm:node/1", "name": "Парк Фасад"}
            option = {"id": "budget-120", "budget_minutes": 120, "route_built": True, "budget_status": "fits",
                      "plan": {"budget_minutes": 120, "total_minutes": 120, "walk_minutes": 20,
                               "visit_minutes": 95, "buffer_minutes": 5, "return_walk_minutes": 10,
                               "verified": True, "returns_to_origin": True}, "stops": [stop]}
            for action, rejected in (("Предлагаю осмотреть фасад.", True),
                                     ("Идея: прогуляться по парку.", True),
                                     ("Предлагаю запланировать посещение места Парк Фасад.", False)):
                with self.subTest(category=category, action=action):
                    value = {"text": "Место соответствует выбранному занятию. " + action + " Условия посещения неизвестны.",
                             "source_ids": ["o0"], "stop_ids": ["osm:node/1"]}
                    calls = []
                    def opened(request, timeout):
                        calls.append(request)
                        return Response({"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})
                    with patch("app.unified_narration.open_local_model", side_effect=AssertionError("No real model HTTP")):
                        answer, status = narrate(grounding, [stop], [option], {"time_of_day": "day"},
                                                 {"categories": [category], "preferences": []}, config, http_open=opened)
                    self.assertEqual(len(calls), 1)
                    if rejected:
                        self.assertIsNone(answer)
                        self.assertEqual(status, "action_does_not_match_single_venue_intent")
                    else:
                        self.assertEqual(answer, value, status)

    def test_all_catalogue_categories_survive_local_model_context_with_full_google_sources(self):
        config = LLMConfig("http://127.0.0.1:8081/v1", "mock-local", "", 120, "schema", "max_tokens")
        grounding = {"summary": "Полный неизменённый ответ Google [0], [1].", "sources": [
            {"id": "google-" + str(index), "source_id": "g" + str(index), "title": "Синтетический Google источник " + str(index),
             "url": "https://www.google.com/maps/?cid=" + str(index), "attribution": {"provider": "Google Maps"}}
            for index in range(2)]}
        original = deepcopy(grounding)
        for category in REQUESTS:
            with self.subTest(category=category):
                stop = {**place("osm:node/1", CATEGORY_CATALOG[category]["source_categories"][0]),
                        "provider": "osm", "source_id": "o0", "stop_id": "osm:node/1"}
                plan = {"id": "budget-120", "budget_minutes": 120, "route_built": True, "budget_status": "fits",
                        "plan": {"budget_minutes": 120, "total_minutes": 60, "walk_minutes": 20, "visit_minutes": 35,
                                 "return_walk_minutes": 10, "buffer_minutes": 5, "verified": True, "returns_to_origin": True}, "stops": [stop]}
                text = ("Кафе соответствует интересу к кофе. Предлагаю сделать паузу на кофе. Условия посещения неизвестны."
                        if category == "cafe" else
                        "Компьютерный клуб соответствует выбранному занятию. Предлагаю игровую паузу после проверки условий. Условия посещения неизвестны."
                        if category == "computer_club" else
                        "Выбранное место соответствует вашему интересу. Предлагаю запланировать посещение после проверки условий. Условия посещения неизвестны.")
                value = {"text": text, "source_ids": ["o0"], "stop_ids": ["osm:node/1"]}
                captured = []
                def opened(request, timeout):
                    captured.append(strict_json(request.data))
                    return Response({"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]})
                with patch("app.unified_narration.open_local_model", side_effect=AssertionError("No real model HTTP")):
                    answer, status = narrate(grounding, [stop], [plan], {"time_of_day": "day", "query": "PRIVATE_ORIGINAL_QUERY"},
                                             {"categories": [category], "preferences": [], "raw_query": "PRIVATE_RAW_INTENT"}, config, http_open=opened)
                self.assertEqual(answer, value, status)
                self.assertEqual(len(captured), 1)
                context = json.loads(captured[0]["messages"][1]["content"])
                self.assertEqual(context["intent"]["categories"], [category])
                self.assertEqual(context["grounding_lite_summary"], grounding["summary"])
                self.assertEqual(len(context["all_google_source_previews"]), 2)
                self.assertNotIn("PRIVATE", json.dumps(captured, ensure_ascii=False))
                self.assertEqual(grounding, original)


if __name__ == "__main__":
    unittest.main()
