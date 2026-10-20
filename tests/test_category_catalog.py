"""Shared leisure vocabulary and bounded parser contract; no external services."""
from types import MappingProxyType
import re
import unittest

from app.activity_intent import SearchIntent, activity_intent, parse_search_intent
from app.category_catalog import CATEGORY_CATALOG, CATEGORY_IDS, categories_from_rubrics, category_from_rubrics


CASES = {
    "walk": ("прогулку", "a walk"),
    "park": ("парк", "a park"),
    "promenade": ("набережную", "a promenade"),
    "playground": ("детскую площадку", "a playground"),
    "cafe": ("кофе", "a cafe"),
    "restaurant": ("ресторан", "a restaurant"),
    "bar": ("бар", "a pub"),
    "museum": ("музей", "a museum"),
    "theatre": ("театр", "a theater"),
    "architecture": ("архитектуру", "architecture"),
    "mall": ("торговый центр", "a mall"),
    "computer_club": ("компьютерный клуб", "a gaming club"),
    "cinema": ("кинотеатр", "a movie theater"),
    "bowling": ("боулинг", "a bowling alley"),
    "billiards": ("бильярд", "a pool hall"),
    "karaoke": ("караоке", "karaoke"),
    "escape_room": ("квест", "an escape room"),
    "board_games": ("настольные игры", "board games"),
    "anti_cafe": ("антикафе", "an anti-cafe"),
    "trampoline": ("батутный центр", "a trampoline park"),
    "climbing": ("скалодром", "a climbing gym"),
    "ice_rink": ("каток", "an ice rink"),
    "pool": ("бассейн", "a swimming pool"),
    "spa": ("спа", "a spa"),
    "amusement_park": ("парк аттракционов", "an amusement park"),
    "fitness": ("фитнес", "a gym"),
    "bookshop": ("книжный магазин", "a bookshop"),
    "library": ("библиотеку", "a library"),
    "coworking": ("коворкинг", "a coworking space"),
    "nightclub": ("ночной клуб", "a nightclub"),
}


class CategoryCatalogTests(unittest.TestCase):
    def test_catalog_is_immutable_and_has_one_shared_complete_spec(self):
        self.assertEqual(set(CASES), CATEGORY_IDS)
        self.assertEqual(len(CATEGORY_CATALOG), 30)
        with self.assertRaises(TypeError):
            CATEGORY_CATALOG["fake"] = {}
        for identifier, spec in CATEGORY_CATALOG.items():
            with self.subTest(identifier=identifier):
                self.assertIsInstance(spec, MappingProxyType)
                self.assertEqual(set(spec), {"ru_label", "google_query", "dgis_query", "mode",
                                            "aliases", "rubric_aliases", "source_categories", "proposed_activity"})
                self.assertIn(spec["mode"], ("walk", "single_venue"))
                for key in ("ru_label", "google_query", "dgis_query", "proposed_activity"):
                    self.assertTrue(spec[key].strip())
                    self.assertNotIn("https:", spec[key])
                for key in ("aliases", "rubric_aliases", "source_categories"):
                    self.assertIsInstance(spec[key], tuple)
                for pattern in spec["aliases"]:
                    re.compile(pattern)
                with self.assertRaises(TypeError):
                    spec["google_query"] = "untrusted"

    def test_only_exact_rubrics_supply_evidence_and_preserve_multiple_actual_types(self):
        self.assertEqual(categories_from_rubrics(["Кафе", "Боулинг", "Кафе", "Памятники"]), ("cafe", "bowling", "monument"))
        self.assertEqual(categories_from_rubrics(["  КОФЕЙНИ ", "Тренажёрные залы"]), ("cafe", "fitness"))
        self.assertEqual(categories_from_rubrics(["Достопримечательности", "Смотровые площадки"]), ("attraction", "viewpoint"))
        self.assertEqual(category_from_rubrics(["Библиотеки", "Коворкинги"]), "library")
        self.assertEqual(categories_from_rubrics(["Настольные игры", "Computer Club Brand", "Магазины игрушек", "Популярные кафе города"]), ())
        self.assertEqual(categories_from_rubrics(["Клубы настольных игр"]), ("board_games",))
        for invalid in (None, {}, "Кафе", 42):
            self.assertEqual(categories_from_rubrics(invalid), ())

    def test_thirty_categories_have_ru_and_en_goals_with_exact_five_field_contract(self):
        for category, phrases in CASES.items():
            for language, phrase in zip(("Хочу ", "I want "), phrases):
                with self.subTest(category=category, phrase=phrase):
                    result = parse_search_intent(language + phrase + " near Baiterek")
                    self.assertEqual(set(result), set(SearchIntent.__annotations__))
                    self.assertTrue(result["supported"])
                    self.assertEqual(result["categories"], [category])
                    self.assertEqual(result["mode"], CATEGORY_CATALOG[category]["mode"])

    def test_longest_compound_match_prevents_false_parent_categories(self):
        for phrase, category in (("pool tables", "billiards"), ("pool halls", "billiards"),
                                 ("movie theater", "cinema"), ("anti-cafe", "anti_cafe"),
                                 ("amusement park", "amusement_park"), ("парк с аттракционами", "amusement_park")):
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_search_intent("Хочу " + phrase)["categories"], [category])
        self.assertFalse(parse_search_intent("Хочу на парковку")["supported"])

    def test_every_activity_uses_same_rejection_scope_and_preserves_new_positive_clause(self):
        for category, phrases in CASES.items():
            requested = "cafe" if category == "museum" else "museum"
            replacement = "кафе" if requested == "cafe" else "музей"
            for prefix in ("Не хочу ", "I don't want ", "I don’t want ", "I do not want "):
                query = prefix + phrases[0] + ", хочу " + replacement
                with self.subTest(category=category, prefix=prefix):
                    result = parse_search_intent(query)
                    self.assertTrue(result["supported"])
                    self.assertEqual(result["categories"], [requested])
                    self.assertIn(category, result["excluded_categories"])
        self.assertEqual(parse_search_intent("Не компьютерный клуб, а кофе")["categories"], ["cafe"])
        self.assertEqual(parse_search_intent("Не только кафе, но и музей")["categories"], ["cafe", "museum"])

    def test_unknown_explicit_goals_fail_before_a_supported_secondary_can_replace_them(self):
        for query in ("Хочу чай", "Хочу пострелять из лука", "Хочу побегать рядом с Байтереком",
                      "Хочу в планетарий и кофе", "Хочу пострелять из лука в кафе", "Хочу в обсерваторию рядом с кафе",
                      "Хочу в интересный планетарий", "find an interesting planetarium near Mega"):
            with self.subTest(query=query):
                result = parse_search_intent(query)
                self.assertFalse(result["supported"])
                self.assertEqual(result["categories"], [])
        self.assertFalse(parse_search_intent("Хочу кофе, но не хочу кофе")["supported"])

    def test_primary_order_and_location_mentions_are_not_category_evidence(self):
        intent = parse_search_intent("Хочу в боулинг и кофе рядом с музеем")
        self.assertEqual(intent["categories"], ["bowling", "cafe"])
        self.assertEqual(intent["mode"], "single_venue")
        self.assertEqual(parse_search_intent("Хочу кофе и боулинг")["categories"], ["cafe", "bowling"])
        self.assertEqual(parse_search_intent("Хочу прогулку в парке")["categories"], ["park"])
        choice = parse_search_intent("Хочу в музей или кафе")
        self.assertEqual(choice["categories"], ["museum", "cafe"])
        self.assertEqual(choice["mode"], "walk")
        self.assertIn("playground", CATEGORY_CATALOG["walk"]["source_categories"])
        self.assertEqual(parse_search_intent("Хочу сыграть в бильярд рядом с Байтереком")["categories"], ["billiards"])
        self.assertEqual(parse_search_intent("Хочу только тихий боулинг")["categories"], ["bowling"])
        self.assertEqual(parse_search_intent("45 минут возле Mega хочу прогулку днём")["categories"], ["walk"])
        bare = parse_search_intent("Кофе и парк")
        self.assertEqual(bare["categories"], ["cafe", "park"])
        self.assertEqual(bare["mode"], "walk")
        self.assertEqual(parse_search_intent("Хочу в музей рядом с кафе")["categories"], ["museum"])
        self.assertEqual(parse_search_intent("Я в кафе, хочу в боулинг")["categories"], ["bowling"])

    def test_family_and_generic_requests_do_not_claim_a_specific_venue_type(self):
        for query in ("Погулять с ребёнком на свежем воздухе", "Я турист, у меня два часа",
                      "найти где погулять возле Mega", "Найти интересное место возле Mega",
                      "find a place near Mega", "хочу необычное рядом с Экспо 2017"):
            with self.subTest(query=query):
                result = parse_search_intent(query)
                self.assertTrue(result["supported"])
                self.assertNotIn("playground", result["categories"])
        self.assertEqual(parse_search_intent("Погулять с ребёнком")["categories"], ["walk"])

    def test_user_required_scenarios_preserve_goals_and_ignore_origin_park(self):
        cases = (
            ("Я сейчас возле Mega Silk Way, хочу прогуляться 30–60 минут — что посоветуете?", ["walk"]),
            ("Хочу тихое место с кофе и розеткой недалеко от байтерека.", ["cafe"]),
            ("Еду с ребёнком в центральный парк — где рядом есть детские площадки или музей с интерактивами?", ["playground", "museum"]),
            ("Я - турист около аэропорта, у меня 2 часа перед вылетом: что можно посмотреть поблизости?", []),
            ("Ищу необычную ночную активность рядом с абу даби.", []),
            ("Я у здании Экспо 2017, хочу что-то необычное на 1–2 часа, пешком.", ["walk"]),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                result = parse_search_intent(query)
                self.assertTrue(result["supported"])
                self.assertEqual(result["categories"], expected)
        self.assertEqual(parse_search_intent(cases[1][0])["preferences"], ["quiet", "socket"])
        self.assertEqual(parse_search_intent(cases[2][0])["mode"], "walk")

    def test_preferences_negation_privacy_and_legacy_wrapper(self):
        result = parse_search_intent("Хочу тихое кафе с розеткой для коляски PRIVATE_NAME 51.1,71.4 https://private.example")
        self.assertEqual(result["preferences"], ["quiet", "socket", "accessible"])
        self.assertEqual(parse_search_intent("Хочу кафе без розетки, тишина не нужна")["preferences"], [])
        serialized = repr(result)
        for private in ("PRIVATE_NAME", "51.1", "private.example"):
            self.assertNotIn(private, serialized)
        self.assertEqual(activity_intent("Хочу боулинг"), {"categories": ["bowling"], "mode": "single_venue", "unsupported": False})
        for query in ("", None, 42):
            self.assertFalse(parse_search_intent(query)["supported"])


if __name__ == "__main__":
    unittest.main()
