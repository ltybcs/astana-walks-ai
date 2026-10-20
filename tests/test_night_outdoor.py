"""Conditional outdoor ideas retain unknown hours; all records are synthetic."""
from copy import deepcopy
import unittest

from app.retrieval import Retriever, detect_intent, night_hours_eligible, requires_confirmed_hours
from app.semantic_intent import validate_intent

ORIGIN = {"lat": 51.09125, "lon": 71.412806}


def poi(identifier, category="park", hours=None, **extra):
    return {"id": identifier, "name": identifier, "category": category, "description": "Синтетическая запись.",
            "lat": ORIGIN["lat"] + .001, "lon": ORIGIN["lon"], "opening_hours": hours,
            "features": {}, "source_url": "https://www.openstreetmap.org/node/1", **extra}


class NightOutdoorTests(unittest.TestCase):
    def test_opt_in_is_only_own_unknown_park_or_promenade_hours(self):
        for category in ("park", "promenade", "cafe", "museum", "playground", "unknown", "attraction"):
            for hours in (None, "", " ", "24/7", "24/7 ", "Mo-Su 00:00-24:00", "closed"):
                place = poi(category, category, hours)
                original = deepcopy(place)
                with self.subTest(category=category, hours=hours):
                    self.assertEqual(night_hours_eligible(place, night=True), hours == "24/7")
                    self.assertEqual(night_hours_eligible(place, night=True, allow_unknown_outdoor_hours=True),
                                     hours == "24/7" or category in {"park", "promenade"} and hours in {None, ""})
                    self.assertEqual(place, original)

    def test_explicit_access_restrictions_never_receive_the_exception(self):
        for access in ("private", "no", " PRIVATE "):
            for field in ("source_tags", "access_evidence"):
                place = poi("restricted", **{field: {"access": access} if field == "source_tags" else access})
                with self.subTest(access=access, field=field):
                    self.assertFalse(night_hours_eligible(place, night=True, allow_unknown_outdoor_hours=True))
                    self.assertFalse(night_hours_eligible({**place, "opening_hours": "24/7"}, night=True, allow_unknown_outdoor_hours=True))

    def test_legacy_stays_strict_but_opt_in_nature_plan_can_select_unchanged_source(self):
        query = "У меня 1,5 часа, я около экспо, хочу увидеть природу Астаны"
        plan = validate_intent({"start_quote": "экспо", "goals": [{"quote": "увидеть природу Астаны",
            "search_text": "парки", "category_hint": "park", "fulfilment": "generic_walk"}],
            "goal_policy": "primary", "excluded": [], "requirements": [], "context": []}, query)
        records = [poi("typed-park"), poi("closed-park", hours="Mo-Su 09:00-18:00"),
                   poi("monument", "monument"), poi("cafe", "cafe")]
        before = deepcopy(records)
        index = Retriever(data={"places": records})
        self.assertEqual(index.retrieve(query, ORIGIN, 90, "night", intent_plan=plan)["selected"], [])
        result = index.retrieve(query, ORIGIN, 90, "night", intent_plan=plan, allow_unknown_outdoor_hours=True)
        self.assertEqual([item["poi"]["id"] for item in result["selected"]], ["typed-park"])
        self.assertIsNone(result["selected"][0]["poi"]["opening_hours"])
        self.assertEqual(records, before)

    def test_hard_features_and_child_filters_are_not_bypassed(self):
        for query in ("Парк с розеткой", "Парк: только тихое", "Парк для коляски"):
            with self.subTest(query=query):
                value = Retriever(data={"places": [poi("park")]}).retrieve(query, ORIGIN, 90, "night", allow_unknown_outdoor_hours=True)
                self.assertEqual(value["selected"], [])
        value = Retriever(data={"places": [poi("bar", "bar", "24/7"), poi("park")]}).retrieve(
            "Прогулка с ребёнком", ORIGIN, 90, "night", allow_unknown_outdoor_hours=True)
        self.assertEqual([item["poi"]["id"] for item in value["selected"]], ["park"])

    def test_explicit_hours_need_own_24x7_even_during_the_day(self):
        positives = ("Только круглосуточный парк ночью", "Парк 24/7", "park open now", "park 24 hours",
                     "Парк открыт сейчас", "Парк должен быть открыт", "Парк обязательно открыт")
        negatives = ("Парк на открытом воздухе", "Не хочу круглосуточный парк", "Круглосуточность не нужна",
                     "park not open now", "Не обязательно открыт сейчас")
        for query in positives:
            self.assertTrue(requires_confirmed_hours(query), query)
        for query in negatives:
            self.assertFalse(requires_confirmed_hours(query), query)
        plan = validate_intent({"start_quote": None, "goals": [{"quote": "парк", "search_text": "парк",
            "category_hint": "park", "fulfilment": "venue_visit"}], "goal_policy": "primary",
            "excluded": [], "requirements": [], "context": []}, "Хочу парк")
        for day in ("day", "night"):
            for semantic in (None, plan):
                flags = detect_intent("Только круглосуточный парк", day, intent_plan=semantic)
                self.assertTrue(flags["require_confirmed_hours"])
                result = Retriever(data={"places": [poi("unknown"), poi("known", hours="24/7")]}).retrieve(
                    "Только круглосуточный парк", ORIGIN, 90, day, intent_plan=semantic, allow_unknown_outdoor_hours=True)
                self.assertEqual([item["poi"]["id"] for item in result["selected"]], ["known"])


if __name__ == "__main__":
    unittest.main()
