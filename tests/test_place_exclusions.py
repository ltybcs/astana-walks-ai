"""Offline literal-name exclusions: no source/model calls or visit history."""
from copy import deepcopy
import unittest

from app.place_exclusions import excluded_place_names, is_place_excluded


class PlaceExclusionTests(unittest.TestCase):
    def test_reverse_russian_visited_list_requires_explicit_pronoun_exclusion(self):
        query = "В Master Coffee и Luna Coffee уже был — не предлагай их. Хочу другую кофейню."
        self.assertEqual(excluded_place_names(query), ("master coffee", "luna coffee"))
        self.assertEqual(excluded_place_names("В Master Coffee и Luna Coffee уже был."), ())
        self.assertEqual(excluded_place_names("В Master Coffee и Luna Coffee я уже был - не предлагай их"),
                         ("master coffee", "luna coffee"))

    def test_forward_russian_visited_list_preserves_named_place_scope(self):
        self.assertEqual(excluded_place_names("Я уже была в Master Coffee и в Luna Coffee, не предлагай их"),
                         ("master coffee", "luna coffee"))
        self.assertEqual(excluded_place_names("Я был в Master Coffee — не рекомендуйте это"), ("master coffee",))

    def test_explicit_russian_named_list_and_command_after_a_clause(self):
        self.assertEqual(excluded_place_names("Не предлагай Master Coffee, Luna Coffee и Geek Game Club"),
                         ("master coffee", "luna coffee", "geek game club"))
        self.assertEqual(excluded_place_names("Я около Mega, пожалуйста, исключи Байтерек и EXPO"), ("байтерек", "expo"))
        self.assertEqual(excluded_place_names("Не предлагай: Master Coffee, Luna Coffee"), ("master coffee", "luna coffee"))

    def test_explicit_english_exclusion_and_visited_list(self):
        self.assertEqual(excluded_place_names("Exclude Master Coffee, Luna Coffee"), ("master coffee", "luna coffee"))
        self.assertEqual(excluded_place_names("Please don't recommend Master Coffee and Luna Coffee."),
                         ("master coffee", "luna coffee"))
        self.assertEqual(excluded_place_names("I've visited Master Coffee and Luna Coffee"),
                         ("master coffee", "luna coffee"))
        self.assertEqual(excluded_place_names("I have already visited Master Coffee, Luna Coffee"),
                         ("master coffee", "luna coffee"))

    def test_quoted_names_preserve_their_commas_and_conjunctions(self):
        names = excluded_place_names('Не предлагай «Coffee and Milk», "Сад, кафе и книги" и Luna Coffee')
        self.assertEqual(names, ("coffee and milk", "сад, кафе и книги", "luna coffee"))
        self.assertTrue(is_place_excluded({"name": "Coffee and Milk"}, names))
        self.assertFalse(is_place_excluded({"name": "Coffee"}, names))
        self.assertFalse(is_place_excluded({"name": "Milk"}, names))

    def test_apostrophes_inside_brands_or_english_grammar_are_not_quotes(self):
        names = excluded_place_names("I've visited O’Coffee and McDonald's")
        self.assertEqual(names, ("o'coffee", "mcdonald's"))
        self.assertTrue(is_place_excluded({"name": "O'Coffee"}, names))
        self.assertEqual(excluded_place_names("Exclude 'O’Coffee', Luna Coffee"), ("o'coffee", "luna coffee"))

    def test_normalization_is_exact_case_unicode_space_quote_and_dash_only(self):
        names = excluded_place_names('Exclude "Ёлка – Coffee", ＭＡＳＴＥＲ  Coffee')
        self.assertEqual(names, ("елка - coffee", "master coffee"))
        self.assertTrue(is_place_excluded({"name": "елка — COFFEE"}, names))
        self.assertTrue(is_place_excluded({"name": '«Master Coffee»'}, names))
        self.assertFalse(is_place_excluded({"name": "Ёлка Coffee"}, names))

    def test_matching_never_uses_substrings_prefixes_or_morphological_stems(self):
        names = excluded_place_names("Не предлагай Mega, Байтерек, Master Coffee")
        for name in ("MEGA Silk Way", "Новый Байтерек", "Байтереке", "Master Coffee EXPO", "Master Coffees"):
            with self.subTest(name=name):
                self.assertFalse(is_place_excluded({"name": name}, names))
        self.assertTrue(is_place_excluded({"name": "Master Coffee"}, names))

    def test_unknown_name_does_not_expand_to_a_category_or_another_place(self):
        names = excluded_place_names("Не предлагай Несуществующий музей")
        self.assertEqual(names, ("несуществующий музей",))
        self.assertFalse(is_place_excluded({"name": "Национальный музей", "category": "museum"}, names))
        self.assertFalse(is_place_excluded({"name": "Сад", "category": "park", "description": "Несуществующий музей"}, names))

    def test_place_own_name_is_required_and_foreign_aliases_or_addresses_are_ignored(self):
        names = excluded_place_names("Exclude Master Coffee")
        place = {"name": "Luna Coffee", "aliases": ["Master Coffee"], "address": "Master Coffee",
                 "title": "Master Coffee", "tags": {"name": "Master Coffee"}, "category": "Master Coffee"}
        self.assertFalse(is_place_excluded(place, names))
        self.assertFalse(is_place_excluded({"title": "Master Coffee"}, names))
        self.assertFalse(is_place_excluded({"name": {"text": "Master Coffee"}}, names))

    def test_generic_category_or_pronoun_is_not_a_named_exclusion(self):
        for query in ("Не предлагай парки", "Не предлагай их", "Не предлагай эти места", "Exclude parks",
                      "Don't suggest these places", "Не предлагай все кафе", "Не предлагай ничего, кроме Master Coffee"):
            with self.subTest(query=query):
                self.assertEqual(excluded_place_names(query), ())

    def test_negated_exclusion_instructions_and_negated_visits_do_not_exclude(self):
        for query in ("Не исключай Master Coffee", "Не надо исключать Master Coffee", "Не хочу говорить не предлагай Master Coffee",
                      "Не говори: не предлагай Master Coffee", "Don't exclude Master Coffee", "Do not exclude Master Coffee",
                      "I have never visited Master Coffee", "I haven't visited Master Coffee", "I've not visited Master Coffee",
                      "В Master Coffee не был — не предлагай их"):
            with self.subTest(query=query):
                self.assertEqual(excluded_place_names(query), ())

    def test_quoted_instruction_is_data_not_a_new_exclusion(self):
        self.assertEqual(excluded_place_names('Фраза "не предлагай Master Coffee" — пример.'), ())
        self.assertEqual(excluded_place_names('Read "Exclude Master Coffee", then walk.'), ())

    def test_positive_request_to_visit_again_is_not_a_visited_exclusion(self):
        for query in ("I've visited Master Coffee, but I'd like to go again", "I have visited Master Coffee and want to revisit it"):
            with self.subTest(query=query):
                self.assertEqual(excluded_place_names(query), ())
        self.assertEqual(excluded_place_names("I've visited Master Coffee, don't exclude it"), ())
        self.assertEqual(excluded_place_names("Don't exclude Master Coffee, I've visited Master Coffee"), ())
        self.assertEqual(excluded_place_names("I've visited Master Coffee. Exclude Luna Coffee"),
                         ("master coffee", "luna coffee"))

    def test_unrelated_goal_clause_does_not_become_an_excluded_name(self):
        self.assertEqual(excluded_place_names("Не предлагай Master Coffee, но Luna Coffee можно. Хочу парк"), ("master coffee",))
        self.assertEqual(excluded_place_names("Exclude Master Coffee, but show parks near Mega"), ("master coffee",))
        self.assertEqual(excluded_place_names("Не предлагай Master Coffee, я там был. Хочу Luna Coffee"), ("master coffee",))

    def test_duplicates_are_removed_in_literal_request_order(self):
        names = excluded_place_names("Не предлагай Ёлка, елка, MASTER Coffee. Exclude master coffee, Luna Coffee")
        self.assertEqual(names, ("елка", "master coffee", "luna coffee"))

    def test_name_and_list_bounds_do_not_truncate_names_into_other_identities(self):
        query = "Exclude " + ", ".join(f"Place {index}" for index in range(12))
        self.assertEqual(excluded_place_names(query), tuple(f"place {index}" for index in range(10)))
        self.assertEqual(excluded_place_names("Exclude " + "A" * 121), ())
        self.assertEqual(excluded_place_names("Exclude " + "A" * 120), ("a" * 120,))
        self.assertFalse(is_place_excluded({"name": "A" * 121}, ("a" * 120,)))
        self.assertEqual(excluded_place_names("Exclude " + "A" * 2001), ())

    def test_invalid_input_or_unbalanced_quotes_do_not_create_exclusions(self):
        for query in (None, 7, {}, [], "", " ", 'Exclude "Master Coffee, Luna Coffee', "Exclude https://example.com"):
            with self.subTest(query=query):
                self.assertEqual(excluded_place_names(query), ())
        for place in (None, [], "Master Coffee", {"name": None}):
            self.assertFalse(is_place_excluded(place, ("master coffee",)))
        for names in (None, "master coffee", {}, tuple(str(index) for index in range(11))):
            self.assertFalse(is_place_excluded({"name": "Master Coffee"}, names))

    def test_source_objects_and_queries_are_unchanged_and_no_history_carries_to_next_call(self):
        query = "Exclude Master Coffee"
        source = {"name": "Master Coffee", "features": {"wifi": "unknown"}}
        before = deepcopy(source)
        names = excluded_place_names(query)
        self.assertTrue(is_place_excluded(source, names))
        self.assertEqual(source, before)
        self.assertEqual(query, "Exclude Master Coffee")
        self.assertEqual(excluded_place_names("Хочу кофе возле Mega"), ())
        self.assertFalse(is_place_excluded(source, ()))
        self.assertTrue(is_place_excluded(source, names))


class SourceTitleCaseFormTests(unittest.TestCase):
    def test_reported_visited_botanical_garden_matches_own_full_nominative_title(self):
        names = excluded_place_names("В Ботаническом саду уже был — не предлагай их")
        self.assertEqual(names, ("ботаническом саду",))
        self.assertTrue(is_place_excluded({"name": "Ботанический сад"}, names))
        for name in ("Президентский парк", "Летний сад", "Ботанический сад EXPO", "Сад", "Другой ботанический сад"):
            with self.subTest(name=name):
                self.assertFalse(is_place_excluded({"name": name}, names))

    def test_closed_full_title_cases_preserve_adjective_and_venue_agreement(self):
        examples = {
            "Центральный парк": ("Центрального парка", "Центральному парку", "Центральным парком", "Центральном парке"),
            "Ботанический сад": ("Ботанического сада", "Ботаническому саду", "Ботаническим садом", "Ботаническом саду"),
            "Национальный музей": ("Национального музея", "Национальному музею", "Национальным музеем", "Национальном музее"),
            "Городская набережная": ("Городской набережной", "Городскую набережную"),
            "Летний сад": ("Летнего сада", "Летнему саду", "Летним садом", "Летнем саду"),
            "Летняя набережная": ("Летней набережной", "Летнюю набережную"),
        }
        for source_name, cases in examples.items():
            for name in cases:
                with self.subTest(source=source_name, form=name):
                    self.assertTrue(is_place_excluded({"name": source_name}, (name,)))
        for name in ("Центральном парка", "Ботаническим саду", "Национального музее", "Городскую набережной"):
            self.assertFalse(is_place_excluded({"name": "Центральный парк"}, (name,)))

    def test_source_title_proper_name_suffix_remains_exact(self):
        self.assertTrue(is_place_excluded({"name": "Парк Первого Президента"}, ("Парке Первого Президента",)))
        self.assertTrue(is_place_excluded({"name": "Музей будущего"}, ("Музее будущего",)))
        self.assertFalse(is_place_excluded({"name": "Парк Жетысу"}, ("Парке Жетысского",)))
        self.assertFalse(is_place_excluded({"name": "Парк Первого Президента"}, ("Парке Президента",)))
        self.assertFalse(is_place_excluded({"name": "Музей искусства"}, ("Музее будущего",)))

    def test_all_adjectives_change_in_the_same_case_without_dropping_full_identity(self):
        source = {"name": "Новый городской парк Жетысу"}
        self.assertTrue(is_place_excluded(source, ("Новом городском парке Жетысу",)))
        self.assertTrue(is_place_excluded(source, ("Новым городским парком Жетысу",)))
        self.assertFalse(is_place_excluded(source, ("Новом городского парке Жетысу",)))
        self.assertFalse(is_place_excluded(source, ("Городском парке Жетысу",)))

    def test_generic_only_unknown_proper_name_and_multihead_titles_are_not_fuzzy_matched(self):
        for source, query in (("Парк", "парке"), ("Сад", "саду"), ("Музей", "музее"),
                              ("Набережная", "набережной"), ("Астана", "Астане"), ("Камал", "Камала"),
                              ("Парк и музей Жетысу", "Парке и музее Жетысу")):
            with self.subTest(source=source, query=query):
                self.assertFalse(is_place_excluded({"name": source}, (query,)))
        self.assertTrue(is_place_excluded({"name": "Камал"}, ("Камал",)))

    def test_source_title_aliases_or_other_sources_cannot_supply_grammatical_identity(self):
        names = ("ботаническом саду",)
        place = {"name": "Президентский парк", "aliases": ["Ботанический сад"],
                 "description": "Ботанический сад", "tags": {"name": "Ботанический сад"}}
        self.assertFalse(is_place_excluded(place, names))


if __name__ == "__main__":
    unittest.main()
