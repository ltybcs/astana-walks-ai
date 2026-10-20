"""Explicit free-day availability, distinct from opening hours and exact budgets."""
import unittest
from datetime import datetime, timezone
from app.lab_context import stage_request


class FreeDayContextTests(unittest.TestCase):
    def context(self, text, **fields):
        return stage_request({'query': text, **fields},
            now=datetime(2026,10,21,9,tzinfo=timezone.utc))['resolved_context']

    def test_whole_day_is_proposed_options_with_an_explicit_interpretation(self):
        for text in ('Мне скучно, у меня есть целый день. Куда можно пойти с Камал-5',
                     'У нас весь день, хочу гулять', 'Весь день свободен, хочу прогулку',
                     'Я свободна целый день, хочу гулять'):
            with self.subTest(text=text):
                ctx=self.context(text)
                self.assertEqual(ctx['budget_options'], [240,360,480])
                self.assertEqual(ctx['budget_source'], 'query_day_options')
                self.assertIsNone(ctx['requested_range'])
                self.assertTrue(any('предложены' in note for note in ctx['assumptions']))
                self.assertEqual(ctx['time_of_day_source'], 'astana_current_time')

    def test_exact_range_and_explicit_budgets_keep_precedence(self):
        for extra, fields, expected in [('У меня есть целый день, но погуляю 90 минут', {}, [90]),
                ('У меня есть целый день, хочу гулять 1–2 часа', {}, [60,90,120]),
                ('У меня есть целый день, хочу гулять', {'minutes':120}, [120])]:
            with self.subTest(text=extra):
                self.assertEqual(self.context(extra, **fields)['budget_options'], expected)

    def test_opening_hours_work_companions_and_negation_are_not_free_time(self):
        for text in ('Хочу кафе, которое работает целый день', 'Кафе открыто весь день',
                     'Я работаю весь день, хочу гулять', 'У меня нет целого дня, хочу гулять',
                     'Не у меня есть целый день, а у друга', 'Весь день не свободен, хочу гулять',
                     'У меня весь день занят работой, хочу прогулку',
                     'У меня целый день не свободен, хочу прогулку',
                     'У меня весь день нет свободного времени, хочу прогулку',
                     'Весь день свободен мой друг, а я хочу прогулку'):
            with self.subTest(text=text):
                self.assertEqual(self.context(text)['budget_options'], [60,120,240])


if __name__ == '__main__':
    unittest.main()
