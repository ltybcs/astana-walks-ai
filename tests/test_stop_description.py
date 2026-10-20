"""Source-backed card resilience; all providers and narration are in-memory fakes."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.route_map import RouteMap
from app.unified_walks import UnifiedWalks, _source_description
from tests.test_unified_walks import Google, poi


class StopDescriptionTests(unittest.TestCase):
    def build(self, *, rejected=False, club=False):
        google, osm, dgis = Google(), Mock(), Mock()
        dgis.status.return_value = {"available": True, "configured": True}
        if club:
            origin = PUBLIC_ORIGINS["abu_dhabi"]
            own = poi("2gis:own", "Синтетический клуб", origin["lat"] + .001, origin["lon"], "computer_club")
            own.update(opening_hours="24/7", description="PRIVATE_OWN_DESCRIPTION_RATING_4.9")
            osm.retrieve.return_value = {"selected": []}
            dgis.search.return_value = {"places": [own], "warnings": []}
            form = {"query": "Хочу в компьютерный клуб на пять часов возле Абу Даби", "minutes": 300,
                    "time_of_day": "night", "consent": True}
        else:
            osm.retrieve.return_value = {"selected": [{"poi": poi("osm:node/1", "Свой парк", 51.09, 71.409)}]}
            dgis.search.return_value = {"places": [poi("2gis:1", "Свой сквер", 51.093, 71.411)], "warnings": []}
            form = {"query": "Хочу прогулку возле Mega", "time_of_day": "day", "consent": True}
        observed = []
        def narration(_grounding, stops, *_):
            if rejected:
                return None, "venue_description_unsupported_number"
            answer = {"text": "Полный принятый текст без замены источников.",
                "stop_ids": [stop["stop_id"] for stop in stops],
                "source_ids": [stop["source_id"] for stop in stops],
                "venue_descriptions": [{"stop_id": stop["stop_id"], "text": stop["name"] + ": собственное описание после проверки условий."}
                                       for stop in stops]}
            observed.append(deepcopy(answer))
            return answer, "mock_validated"
        narrator = Mock(side_effect=narration)
        service = UnifiedWalks(resolve_request, osm, google, dgis, narrator=narrator,
                               route_map=RouteMap("PUBLIC_TEST_EMBED_KEY"))
        return service, form, narrator, observed

    def test_whole_model_rejection_keeps_ready_source_description_and_honest_status(self):
        service, form, narrator, _ = self.build(rejected=True, club=True)
        result = service.recommend(form)
        self.assertEqual(result["mode"], "grounding-only")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["diagnostics"]["llm_status"], "venue_description_unsupported_number")
        self.assertEqual(result["diagnostics"]["llm_attempts"], 1)
        narrator.assert_called_once()
        stop = result["alternatives"][0]["stops"][0]
        self.assertEqual(stop["description_origin"], "source-template")
        text = stop["user_description"]
        self.assertIn("Синтетический клуб", text)
        self.assertIn("Идея: запланировать игровую паузу", text)
        self.assertIn("В источнике 2ГИС указан круглосуточный режим.", text)
        for unsupported in ("4.9", "PRIVATE_", "мощные", "открыт сейчас", "275"):
            self.assertNotIn(unsupported, text)
        self.assertTrue(result["alternatives"][0]["route_built"])
        self.assertEqual(result["alternatives"][0]["budget_status"], "fits")
        self.assertTrue(result["alternatives"][0]["map_ticket"])

    def test_accepted_model_text_overrides_only_the_exact_stop_identity(self):
        service, form, narrator, observed = self.build()
        result = service.recommend(form)
        narrator.assert_called_once()
        self.assertEqual(result["answer"], observed[0])
        self.assertEqual(result["mode"], "llm-grounded-unified")
        self.assertEqual(result["diagnostics"]["llm_status"], "mock_validated")
        expected = {item["stop_id"]: item["text"] for item in observed[0]["venue_descriptions"]}
        self.assertGreater(len(expected), 1)
        for option in result["alternatives"]:
            for stop in option["stops"]:
                self.assertEqual(stop["description_origin"], "local-model")
                self.assertEqual(stop["user_description"], expected[stop["stop_id"]])

    def test_template_uses_only_own_type_not_other_description_or_search_scope(self):
        stop = {"name": "Своя кофейня", "category": "cafe", "provider": "osm", "opening_hours": None,
                "description": "Чужой рейтинг 4.9, уютное заведение с мощной техникой.",
                "features": {"quiet": "yes"}, "source_rubrics": ["Кафе"]}
        before = deepcopy(stop)
        text = _source_description(stop)
        self.assertIn("Своя кофейня", text)
        self.assertIn("паузу на кофе", text)
        for unsupported in ("рейтинг", "4.9", "уют", "мощн", "тих", "круглосуточ"):
            self.assertNotIn(unsupported, text)
        self.assertEqual(stop, before)
        unknown = {**stop, "category": "unknown", "source_rubrics": ["Планетарии"]}
        text = _source_description(unknown)
        self.assertIn("Идея: запланировать посещение после проверки условий.", text)
        for inferred in ("паузу на кофе", "звезд", "экспозиц", "круглосуточ"):
            self.assertNotIn(inferred, text)

    def test_hours_fact_requires_exact_own_24x7_and_template_stays_bounded(self):
        stop = {"name": "Очень длинное имя " * 50, "category": "park", "provider": "google"}
        for hours in (None, "", "24/7 ", "Mo-Su 00:00-24:00", {"is_24x7": True}):
            with self.subTest(hours=hours):
                self.assertNotIn("круглосуточ", _source_description({**stop, "opening_hours": hours}))
        text = _source_description({**stop, "opening_hours": "24/7"})
        self.assertIn("В источнике Google Maps указан круглосуточный режим.", text)
        self.assertLessEqual(len(text), 600)
        self.assertIn("…", text)


if __name__ == "__main__":
    unittest.main()
