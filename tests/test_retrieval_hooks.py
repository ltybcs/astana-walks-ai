"""Cooperative local source deadline and injected own-schedule eligibility."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app.retrieval import Retriever, night_hours_eligible


ASTANA=timezone(timedelta(hours=5))
ORIGIN={"lat":51.0892,"lon":71.4074}


def place(identifier="one", category="park", hours=None, **extra):
    return {"id":identifier,"name":"Синтетический " + identifier,"category":category,
            "description":"Наблюдение тестового источника.","lat":51.0895,"lon":71.4077,
            "opening_hours":hours,"features":{},"source_url":"https://example.test/source",**extra}


class RetrievalHookTests(unittest.TestCase):
    def test_known_current_schedule_opens_non_outdoor_at_night_and_rejects_closed_daytime(self):
        cafe=place(category="cafe",hours="Mo-Fr 09:00-18:00")
        original=deepcopy(cafe)
        opening=datetime(2026,10,21,10,0,tzinfo=ASTANA)
        closed=datetime(2026,10,21,19,0,tzinfo=ASTANA)
        self.assertTrue(night_hours_eligible(cafe,night=True,schedule_when=opening))
        self.assertFalse(night_hours_eligible(cafe,night=False,schedule_when=closed))
        self.assertEqual(cafe,original)

    def test_unknown_or_uninjected_schedule_keeps_existing_night_policy(self):
        when=datetime(2026,10,21,10,0,tzinfo=ASTANA)
        unknown=place(hours=None)
        self.assertFalse(night_hours_eligible(unknown,night=True,schedule_when=when))
        self.assertTrue(night_hours_eligible(unknown,night=True,schedule_when=when,allow_unknown_outdoor_hours=True))
        unsupported=place(hours="Mo-Fr 09:00-18:00; PH off")
        self.assertFalse(night_hours_eligible(unsupported,night=True,schedule_when=when,allow_unknown_outdoor_hours=True))
        self.assertTrue(night_hours_eligible(unsupported,night=False,schedule_when=when))
        self.assertFalse(night_hours_eligible(place(category="cafe",hours="Mo-Fr 09:00-18:00"),night=True))
        self.assertFalse(night_hours_eligible(place(category="cafe"),night=True,schedule_when=when,allow_unknown_outdoor_hours=True))

    def test_private_and_mandatory_24_hours_are_not_relaxed_by_weekly_open(self):
        when=datetime(2026,10,21,10,0,tzinfo=ASTANA)
        weekly=place(hours="Mo-Fr 09:00-18:00")
        self.assertFalse(night_hours_eligible(weekly,night=False,schedule_when=when,require_confirmed_hours=True))
        self.assertTrue(night_hours_eligible(place(hours="24/7"),night=True,schedule_when=when,require_confirmed_hours=True))
        for field in ({"source_tags":{"access":"private"}}, {"access_evidence":"no"}):
            self.assertFalse(night_hours_eligible(place(hours="24/7",**field),night=False,schedule_when=when))

    def test_actual_retriever_uses_injected_schedule_for_day_and_night_without_mutation(self):
        records=[place("open",category="cafe",hours="Mo-Fr 09:00-18:00"),
                 place("closed",category="cafe",hours="Mo-Fr 20:00-22:00")]
        original=deepcopy(records)
        retriever=Retriever(data={"places":records})
        when=datetime(2026,10,21,10,0,tzinfo=ASTANA)
        for day in ("day","night"):
            result=retriever.retrieve("Хочу кафе",ORIGIN,60,day,schedule_when=when)
            self.assertEqual([r["poi"]["id"] for r in result["selected"]],["open"])
        self.assertEqual(records,original)

    def test_expired_deadline_stops_before_interpretation_and_does_not_return_partial(self):
        retriever=Retriever(data={"places":[place()]})
        with patch("app.retrieval.time.monotonic",return_value=10), patch("app.retrieval.detect_intent",side_effect=AssertionError("No interpretation")):
            with self.assertRaisesRegex(TimeoutError,"Source retrieval deadline exceeded"):
                retriever.retrieve("PRIVATE_SENTINEL",ORIGIN,60,deadline=10)

    def test_scan_cooperatively_stops_after_expensive_candidate_work(self):
        records=[place(str(index)) for index in range(30)]
        original=deepcopy(records)
        retriever=Retriever(data={"places":records})
        clock=[0.0]
        called=[]
        def expensive(index,terms):
            called.append(index)
            clock[0]=20.0
            return 1.0
        with patch("app.retrieval.time.monotonic",side_effect=lambda:clock[0]), patch.object(retriever,"lexical_score",side_effect=expensive):
            with self.assertRaises(TimeoutError):
                retriever.retrieve("Хочу парк",ORIGIN,60,deadline=10.0)
        self.assertEqual(called,[0])
        self.assertEqual(records,original)

    def test_invalid_deadline_fails_without_scanning_and_future_deadline_keeps_plan(self):
        retriever=Retriever(data={"places":[place()]})
        for deadline in (True,"10",float("nan"),float("inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                retriever.retrieve("Хочу парк",ORIGIN,60,deadline=deadline)
        ordinary=retriever.retrieve("Хочу парк",ORIGIN,60)
        with patch("app.retrieval.time.monotonic",return_value=1.0):
            bounded=retriever.retrieve("Хочу парк",ORIGIN,60,deadline=10.0)
        self.assertEqual(bounded,ordinary)


if __name__=="__main__":
    unittest.main()
