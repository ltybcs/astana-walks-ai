"""Pure weekly-schedule tests; all clocks injected, no live opening claims."""
from datetime import datetime, timedelta, timezone
import unittest

from app.opening_hours import evaluate_schedule

LOCAL = timezone(timedelta(hours=5))
MONDAY = datetime(2026, 10, 19, tzinfo=LOCAL)


def at(day=0, hour=0, minute=0, second=0):
    return MONDAY + timedelta(days=day, hours=hour, minutes=minute, seconds=second)


def dgis(**overrides):
    values = {name: "09:00–18:00" for name in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    values.update(overrides)
    return "; ".join(name + " " + hours for name, hours in values.items())


class OpeningHoursTests(unittest.TestCase):
    def status(self, hours, when, expected, duration=0):
        result = evaluate_schedule(hours, when, duration_minutes=duration)
        self.assertEqual(result["status"], expected, (hours, when, duration, result))
        self.assertEqual(set(result), {"status", "reason"})
        return result

    def test_exact_24_7_covers_point_and_full_week_without_live_claim(self):
        self.status("24/7", at(6, 23, 59, 59), "open")
        result = self.status("24/7", at(2, 11), "open", 10080)
        self.assertEqual(result["reason"], "reported_schedule_covers_interval")
        self.status("24/7; PH off", at(2, 11), "unknown")

    def test_weekdays_and_partial_osm_week_leave_unlisted_days_closed(self):
        for day in range(5):
            self.status("Mo-Fr 09:00-18:00", at(day, 12), "open")
        for day in (5, 6):
            self.status("Mo-Fr 09:00-18:00", at(day, 12), "closed")
        self.status("We 10:00-12:00", at(0, 11), "closed")
        self.status("We 10:00-12:00", at(2, 11), "open")

    def test_day_lists_wrapping_ranges_and_daily_without_selector(self):
        self.status("Mo,We,Fr 09:00-18:00", at(2, 12), "open")
        self.status("Mo,We,Fr 09:00-18:00", at(1, 12), "closed")
        self.status("Fr-Mo 10:00-11:00", at(6, 10, 30), "open")
        self.status("Fr-Mo 10:00-11:00", at(1, 10, 30), "closed")
        self.status("09:00-18:00", at(6, 12), "open")

    def test_opening_is_inclusive_and_closing_point_is_exclusive(self):
        self.status("Mo 09:00-18:00", at(0, 8, 59, 59), "closed")
        self.status("Mo 09:00-18:00", at(0, 9), "open")
        self.status("Mo 09:00-18:00", at(0, 17, 59, 59), "open")
        self.status("Mo 09:00-18:00", at(0, 18), "closed")

    def test_visit_may_end_at_closing_but_not_extend_beyond_it(self):
        self.status("Mo 09:00-18:00", at(0, 17), "open", 60)
        self.status("Mo 09:00-18:00", at(0, 17), "closed", 60.01)
        self.status("Mo 09:00-18:00", at(0, 17, 59, 59), "open", 1 / 60)
        self.status("Mo 09:00-18:00", at(0, 17, 59, 59), "closed", 2 / 60)

    def test_all_visit_minutes_must_be_covered_not_only_both_endpoints(self):
        hours = "Mo 09:00-12:00,13:00-18:00"
        self.status(hours, at(0, 11), "open", 60)
        self.status(hours, at(0, 11), "closed", 180)
        self.status(hours, at(0, 12), "closed")
        self.status(hours, at(0, 13), "open")

    def test_adjacent_intervals_cover_continuous_visit(self):
        self.status("Mo 09:00-12:00,12:00-18:00", at(0, 11), "open", 120)
        self.status("Mo 00:00-24:00; Tu 00:00-24:00", at(0, 23), "open", 120)

    def test_overnight_interval_carries_to_following_day(self):
        hours = "Mo 22:00-02:00"
        self.status(hours, at(0, 23), "open", 120)
        self.status(hours, at(1, 1), "open", 60)
        self.status(hours, at(1, 2), "closed")
        self.status(hours, at(0, 21, 59), "closed")
        self.status("Mo-Fr 22:00-02:00", at(3, 1), "open")

    def test_sunday_overnight_crosses_week_boundary(self):
        self.status("Su 22:00-02:00", at(0, 1), "open")
        self.status("Su 22:00-02:00", at(6, 23), "open", 180)
        self.status("Su 22:00-02:00", at(6, 23), "closed", 181)

    def test_semicolon_replaces_earlier_hours_on_same_calendar_day(self):
        hours = "Mo-Sa 09:00-12:00; We 15:00-18:00"
        self.status(hours, at(2, 10), "closed")
        self.status(hours, at(2, 16), "open")
        self.status(hours, at(1, 10), "open")
        # Official OSM caution: later Tuesday normal rule removes Monday's
        # spill into early Tuesday; a comma additional rule would differ.
        self.status("Mo 20:00-02:00; Tu 20:00-24:00", at(1, 1), "closed")
        self.status("Mo 20:00-02:00; Tu 20:00-24:00", at(0, 23), "open")
        self.status("Mo 20:00-02:00; Tu 20:00-24:00", at(1, 21), "open")

    def test_explicit_off_and_closed_override_weekly_hours(self):
        hours = "Mo-Su 09:00-18:00; Su off"
        self.status(hours, at(6, 12), "closed")
        self.status(hours, at(5, 12), "open")
        self.status("off", at(0, 12), "closed")
        self.status("closed", at(6, 12), "closed")
        self.status("Su 22:00-02:00; Mo off", at(0, 1), "closed")

    def test_unsupported_overnight_start_day_redefinition_is_not_guessed(self):
        for hours in ("Mo 22:00-02:00; Mo off", "Mo-Fr 22:00-02:00; We 10:00-12:00"):
            self.status(hours, at(0, 23), "unknown")

    def test_2gis_complete_normalized_week_and_multiple_intervals(self):
        hours = dgis(mon="09:00–12:00, 13:00–18:00", sun="off")
        self.status(hours, at(0, 10), "open")
        self.status(hours, at(0, 12), "closed")
        self.status(hours, at(0, 11), "closed", 180)
        self.status(hours, at(6, 10), "closed")
        self.status(dgis(mon="00:00–24:00"), at(0, 23), "open")

    def test_actual_collector_capitalized_week_evaluates_with_own_lunch_break(self):
        from scripts.collect_2gis import DAYS, opening_hours
        item = {"schedule": {day: {"working_hours": [
            {"from": "09:00", "to": "12:00"}, {"from": "13:00", "to": "18:00"}]} for day in DAYS}}
        hours = opening_hours(item)
        self.assertTrue(hours.startswith("Mon "))
        self.assertIn("; Sun ", hours)
        self.status(hours, at(0, 10), "open")
        self.status(hours, at(0, 12), "closed")
        self.status(hours, at(6, 14), "open")
        self.status(hours, at(0, 11), "closed", 180)
        # Mixed casing is still one day identity; it cannot hide a duplicate
        # Monday in place of the missing Sunday.
        self.status(hours.replace("Sun ", "mon "), at(0, 10), "unknown")

    def test_2gis_overnight_records_are_combined_not_osm_normal_overrides(self):
        hours = dgis(mon="22:00–02:00", tue="22:00–02:00", sun="22:00–02:00")
        self.status(hours, at(1, 1), "open")
        self.status(hours, at(0, 1), "open")
        self.status(hours, at(6, 23), "open", 120)

    def test_incomplete_duplicate_or_mixed_2gis_form_is_unknown(self):
        for hours in ("mon 09:00–18:00", dgis().replace("sun ", "sat "),
                      dgis().replace("sun ", "Su ")):
            self.status(hours, at(0, 12), "unknown")

    def test_injected_instant_is_always_converted_to_astana_utc_plus_five(self):
        # Sunday19:00UTC is Monday00:00 in Astana, not Sunday local.
        instant = datetime(2026, 10, 18, 19, tzinfo=timezone.utc)
        self.status("Mo 00:00-01:00", instant, "open")
        self.status("Su 19:00-20:00", instant, "closed")
        same = instant.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(evaluate_schedule("Mo 00:00-01:00", instant),
                         evaluate_schedule("Mo 00:00-01:00", same))

    def test_holidays_dates_comments_specials_make_whole_schedule_unknown(self):
        suffixes = ("PH off", "SH off", "Dec 25 off", "2026 Oct 21 off", "week 01-53 10:00-12:00",
                    '"by appointment"', "есть особое расписание — проверьте карточку")
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                self.status("Mo-Su 09:00-18:00; " + suffix, at(0, 12), "unknown")
        for hours in ('Mo 09:00-18:00 "quiet room"', 'Mo 09:00-18:00 || "call us"',
                      "sunrise-sunset", "Mo 10:00+", "Mo 10:00-18:00 unknown", "Mo[1] 09:00-18:00"):
            self.status(hours, at(0, 12), "unknown")

    def test_additional_rule_comma_is_unsupported_not_treated_as_semicolon(self):
        self.status("Mo 20:00-02:00, Tu 20:00-24:00", at(1, 1), "unknown")
        self.status("Mo 09:00-12:00, We 15:00-18:00", at(2, 16), "unknown")

    def test_malformed_and_unsupported_extended_intervals_never_open(self):
        for hours in ("Mo 24:00-01:00", "Mo 20:00-26:00", "Mo 10:60-12:00", "Mo 09:00-25:00",
                      "Mo 00:00-00:00", "Mo 9:00-18:00", "Mo 09:00", "Mo-Fr 09:00-18:00;",
                      "Mo 09:00-18:00;;Tu off", "monday 09:00-18:00", "mo 09:00-18:00", "Mo 09:00-18:00\n", "Mo ٠٩:٠٠-١٨:٠٠"):
            with self.subTest(hours=hours):
                self.status(hours, at(0, 12), "unknown")

    def test_invalid_or_imprecise_instant_and_invalid_duration_are_unknown(self):
        for when in (None, "tomorrow", "2026-10-21", datetime(2026, 10, 21), True):
            self.status("24/7", when, "unknown")
        for duration in (-1, True, None, "60", float("nan"), float("inf"), 10081, 10 ** 1000):
            result = self.status("24/7", at(), "unknown", duration)
            self.assertEqual(result["reason"], "invalid_duration")

    def test_missing_invalid_shapes_and_bounds_return_only_static_reason(self):
        for hours in (None, "", "   ", [], {}, True, 123, "x" * 2049,
                      "; ".join(["Mo 09:00-18:00"] * 33)):
            result = self.status(hours, at(0, 12), "unknown")
            self.assertIn(result["reason"], {"missing_schedule", "unsupported_schedule"})
            self.assertNotIn("09:00", result["reason"])
            self.assertNotIn("x" * 20, result["reason"])


if __name__ == "__main__":
    unittest.main()
