from app.place_exclusions import mask_place_exclusions
import unittest

class ExclusionMaskTests(unittest.TestCase):
    def test_named_clauses_are_masked_but_goals_and_category_exclusions_remain(self):
        for clause in ('Не предлагай «Ботанический сад».', 'Не предлагай Ботанический сад.',
                       'В Ботаническом саду уже был — не предлагай их', 'Exclude Botanical Garden.'):
            with self.subTest(clause=clause):
                query='Хочу парки без баров. '+clause
                masked=mask_place_exclusions(query)
                self.assertIn('Хочу парки без баров.',masked)
                self.assertNotIn('Ботаническ',masked)
                self.assertNotIn('Botanical',masked)

    def test_ambiguous_category_property_and_mixed_lists_are_never_erased(self):
        for query in ('не предлагай кафе с розеткой', 'не предлагай тихие парки',
                      'не предлагай в парки', 'Не предлагай красивые парки',
                      'не предлагай Botanical Garden и кафе с розеткой',
                      'Не предлагай парки', 'Не предлагай «парки»'):
            with self.subTest(query=query):
                self.assertEqual(mask_place_exclusions(query),query)

    def test_every_bare_catalog_rubric_and_known_english_categories_remain_categories(self):
        for name in ('bars','бары','Бары','pubs','ночные клубы','компьютерные клубы','pharmacies','cinemas','боулинг'):
            for quoted in (False,True):
                query='I want a cafe. Exclude '+('"'+name+'"' if quoted else name)
                with self.subTest(name=name,quoted=quoted):
                    self.assertEqual(mask_place_exclusions(query),query)

if __name__=='__main__':unittest.main()
