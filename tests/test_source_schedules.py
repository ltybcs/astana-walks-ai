from datetime import datetime, timezone
import unittest
from scripts.collect_2gis import opening_hours, DAYS
from app.opening_hours import evaluate_schedule


class SourceScheduleTests(unittest.TestCase):
    def item(self):
        return {"schedule": {day:{"working_hours":[{"from":"09:00","to":"18:00"}]} for day in DAYS}}

    def test_collector_preserves_uncertainty_without_raw_conditional_content(self):
        for key in ("date_from", "date_to", "comment", "description", "schedule_special"):
            with self.subTest(key=key):
                item=self.item()
                if key=="schedule_special":item[key]={"PRIVATE_RAW":"PRIVATE_RAW"}
                else:item["schedule"][key]="PRIVATE_RAW"
                normalized=opening_hours(item)
                self.assertNotIn("PRIVATE_RAW",normalized)
                self.assertEqual(evaluate_schedule(normalized,datetime(2026,10,19,5,tzinfo=timezone.utc))["status"],"unknown")

    def test_unconditional_seven_day_schedule_remains_evaluable(self):
        normalized=opening_hours(self.item())
        self.assertEqual(evaluate_schedule(normalized,datetime(2026,10,19,5,tzinfo=timezone.utc))["status"],"open")
        self.assertEqual(evaluate_schedule(normalized,datetime(2026,10,19,15,tzinfo=timezone.utc))["status"],"closed")
