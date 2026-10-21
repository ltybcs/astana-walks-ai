"""Offline schedule/deadline orchestration; synthetic clocks and providers only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.google_budget import GoogleError
from app.lab_context import PUBLIC_ORIGINS, resolve_request
from app.retrieval import detect_intent
from app.semantic_intent import validate_intent
from app.unified_walks import UnifiedWalks, _eligible
from tests.test_unified_walks import Google, poi

NOW = datetime(2026, 10, 19, 5, tzinfo=timezone.utc)  # Monday10:00 Astana.


class ScheduleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.google = Google()
        self.osm, self.dgis, self.map = Mock(), Mock(), Mock()
        self.dgis.status.return_value = {"available": True}
        self.dgis.search.return_value = {"places": [], "warnings": []}
        self.map.issue.return_value = "synthetic-map-ticket"
        self.place = poi("osm:node/1", "Synthetic cafe", 51.09, 71.409, "cafe")
        self.place["opening_hours"] = "Mo 09:00-18:00"
        self.osm.retrieve.return_value = {"selected": [{"poi": deepcopy(self.place)}]}
        self.narrator = Mock(return_value=(None, "local_model_unconfigured"))
        self.clock = Mock(return_value=NOW)
        self.service = UnifiedWalks(lambda form: resolve_request(form, now=NOW), self.osm,
            self.google, self.dgis, narrator=self.narrator, route_map=self.map, clock=self.clock)
        self.form = {"query": "Хочу кафе возле Mega", "consent": True}

    def provide(self, *, hours=None, category="cafe"):
        place = {**deepcopy(self.place), "opening_hours": hours, "category": category}
        self.osm.retrieve.return_value = {"selected": [{"poi": place}]}
        return place

    def test_current_schedule_reaches_source_hooks_and_full_ready_visit_status(self):
        result = self.service.recommend({**self.form, "minutes": 60})
        option = result["alternatives"][0]
        self.assertTrue(option["route_built"])
        self.assertEqual(option["stops"][0]["opening_status"], {
            "status": "open", "reason": "reported_schedule_covers_interval", "source": "source-reported",
            "note": "Указанное расписание покрывает предложенное посещение; фактический доступ не проверен."})
        self.assertEqual(option["plan"]["walk_minutes"], 4)
        self.assertEqual(option["plan"]["visit_minutes"], 51)
        self.assertEqual(self.osm.retrieve.call_args.kwargs["schedule_when"], NOW)
        self.assertEqual(self.osm.retrieve.call_args.kwargs["deadline"], self.dgis.search.call_args.kwargs["deadline"])
        self.assertTrue(result["search_complete"])
        self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")
        self.assertEqual(len(result["grounding"]["sources"]), 1)
        self.assertEqual(self.narrator.call_args.args[3]["source_availability"],
                         {"osm": "completed", "google": "completed", "dgis": "completed"})

    def test_source_schedule_that_closes_during_visit_never_gets_ready_plan_or_map(self):
        self.provide(hours="Mo 09:00-11:00")
        result = self.service.recommend({**self.form, "minutes": 120})
        option = result["alternatives"][0]
        self.assertFalse(option["route_built"])
        self.assertFalse(option["plan"]["verified"])
        self.assertEqual(option["stops"], [])
        self.assertIsNone(option["map_ticket"])
        self.narrator.assert_not_called()
        self.map.issue.assert_not_called()
        self.assertEqual(result["source_receipt"]["osm"]["candidate_count"], 1)
        self.assertEqual(option["plan"]["schedule_reason"], "reported_schedule_does_not_cover_interval")
        self.assertTrue(any("указанное расписание не покрывает" in warning for warning in result["warnings"]))

    def test_budgets_are_evaluated_separately_and_do_not_borrow_shorter_visit_status(self):
        self.provide(hours="Mo 09:00-11:30")
        result = self.service.recommend(self.form)
        self.assertEqual([o["budget_minutes"] for o in result["alternatives"]], [60, 120, 240])
        self.assertEqual([o["route_built"] for o in result["alternatives"]], [True, False, False])
        self.assertEqual(self.map.issue.call_count, 1)
        self.assertEqual(result["alternatives"][0]["stops"][0]["opening_status"]["status"], "open")

    def test_arrival_after_closing_is_not_treated_as_open_at_departure(self):
        self.provide(hours="Mo 09:00-10:01")
        result = self.service.recommend({**self.form, "minutes": 60})
        self.assertFalse(result["alternatives"][0]["route_built"])
        self.assertEqual(result["alternatives"][0]["plan"]["route_checks"][0]["error_code"], "outside_reported_schedule")
        self.map.issue.assert_not_called()
        self.narrator.assert_not_called()

    def test_previous_walking_and_visits_shift_next_arrival_before_full_coverage_check(self):
        first = {**deepcopy(self.place), "category": "park", "stop_id": "osm:first", "opening_hours": "Mo 09:00-18:00"}
        second = {**deepcopy(first), "id": "osm:node/2", "stop_id": "osm:second", "opening_hours": "Mo 10:00-10:25"}
        option = self.service._plans([first, second], [120], {}, PUBLIC_ORIGINS["mega"], schedule_when=NOW)[0]
        # First arrival10:02, visit20, second arrival10:24: its proposed20min
        # extends past10:25. Ignoring the prior visit would wrongly accept it.
        self.assertEqual([stop["stop_id"] for stop in option["stops"]], ["osm:first"])
        self.assertEqual(option["plan"]["visit_minutes"], 20)
        self.assertEqual(len(self.google.legs), 4)  # No additional route calls.

    def test_known_closed_first_candidate_does_not_move_next_candidate_start(self):
        first = {**deepcopy(self.place), "stop_id": "osm:first", "opening_hours": "Mo 09:00-10:01"}
        second = {**deepcopy(first), "id": "osm:node/2", "stop_id": "osm:second", "opening_hours": "Mo 09:00-18:00", "lat": 51.091}
        option = self.service._plans([first, second], [60], {"activity_mode": "single_venue"},
                                     PUBLIC_ORIGINS["mega"], schedule_when=NOW)[0]
        self.assertEqual([stop["stop_id"] for stop in option["stops"]], ["osm:second"])
        self.assertEqual(self.google.legs[2][0], self.google.legs[0][0])
        self.assertEqual(len(self.google.legs), 4)

    def test_unknown_schedule_remains_unknown_not_an_open_claim(self):
        self.provide(hours="Mo 09:00-18:00; PH off")
        result = self.service.recommend({**self.form, "minutes": 60})
        stop = result["alternatives"][0]["stops"][0]
        self.assertEqual(stop["opening_status"]["status"], "unknown")
        self.assertEqual(stop["opening_status"]["reason"], "unsupported_schedule")
        self.assertIn("не подтверждено", stop["opening_status"]["note"])
        self.assertEqual(stop["opening_hours"], "Mo 09:00-18:00; PH off")

    def test_explicit_future_period_clock_time_and_legacy_period_do_not_invent_instant(self):
        for query, extra in (("Хочу кафе завтра возле Mega", {}),
                             ("Хочу кафе в пятницу возле Mega", {}),
                             ("Хочу кафе в 15:00 возле Mega", {}),
                             ("Хочу кафе в декабре возле Mega", {}),
                             ("Хочу кафе возле Mega", {"time_of_day": "day"})):
            with self.subTest(query=query, extra=extra):
                self.clock.reset_mock()
                self.provide(hours="Mo 09:00-18:00")
                result = self.service.recommend({"query": query, "minutes": 60, "consent": True, **extra})
                self.clock.assert_not_called()
                self.assertIsNone(self.osm.retrieve.call_args.kwargs["schedule_when"])
                stop = result["alternatives"][0]["stops"][0]
                self.assertEqual(stop["opening_status"]["status"], "unknown")
                self.assertEqual(stop["opening_status"]["reason"], "imprecise_visit_time")

    def test_client_current_time_source_uses_one_injected_server_instant(self):
        result = self.service.recommend({**self.form, "minutes": 60,
            "client_context": {"instant": NOW.isoformat(), "utc_offset_minutes": 300}})
        self.assertEqual(result["resolved_context"]["time_of_day_source"], "client_current_time")
        self.assertEqual(self.osm.retrieve.call_args.kwargs["schedule_when"], NOW)
        self.assertEqual(self.clock.call_count, 1)

    def test_current_night_weekly_cafe_can_fit_but_confirmed_hours_demand_stays_strict(self):
        night = NOW + timedelta(hours=12)
        self.service.resolver = lambda form: resolve_request(form, now=night)
        self.clock.return_value = night
        self.provide(hours="Mo 21:00-23:00")
        result = self.service.recommend({**self.form, "minutes": 60})
        self.assertTrue(result["alternatives"][0]["route_built"])
        self.assertEqual(result["alternatives"][0]["stops"][0]["opening_status"]["status"], "open")
        self.narrator.reset_mock()
        result = self.service.recommend({"query": "Хочу кафе обязательно открыто сейчас возле Mega", "minutes": 60, "consent": True})
        self.assertFalse(result["alternatives"][0]["route_built"])
        self.narrator.assert_not_called()

    def test_overnight_visit_crossing_sunday_week_boundary_uses_same_interval_rules(self):
        sunday = NOW + timedelta(days=6, hours=13, minutes=30)  # Sunday23:30local.
        stop = {**deepcopy(self.place), "stop_id": "osm:night", "opening_hours": "Su 23:00-01:00"}
        options = self.service._plans([stop], [30, 120], {"activity_mode": "single_venue"},
                                     PUBLIC_ORIGINS["mega"], schedule_when=sunday)
        self.assertEqual([o["route_built"] for o in options], [True, False])

    def test_planned_or_branch_owns_type_checks_instead_of_duplicate_global_filters(self):
        query = "Хочу прогулку или компьютерный клуб возле Mega"
        plan = validate_intent({"start_quote": "Mega", "goals": [
            {"quote": "прогулку", "search_text": "прогулка", "category_hint": "walk", "fulfilment": "generic_walk"},
            {"quote": "компьютерный клуб", "search_text": "компьютерный клуб", "category_hint": "computer_club", "fulfilment": "venue_visit"}],
            "excluded": [], "goal_policy": "any", "requirements": [], "context": []}, query)
        intent = detect_intent(query, "day", intent_plan=plan)
        for category in ("park", "computer_club"):
            place = {**deepcopy(self.place), "category": category, "opening_hours": "24/7"}
            self.assertTrue(_eligible(place, intent, plan, schedule_when=NOW))


class InlineFuture:
    def __init__(self, executor, provider, value=None, error=None):
        self.executor, self.provider, self.value, self.error = executor, provider, value, error
        self.cancelled = False
    def result(self, timeout):
        self.executor.waits.append((self.provider, timeout))
        self.executor.on_wait(self.provider)
        if self.provider in self.executor.timeouts:
            raise TimeoutError("PRIVATE_SOURCE_TIMEOUT")
        if self.error:
            raise self.error
        return self.value
    def cancel(self):
        self.cancelled = True
        return True


class InlineExecutor:
    """Mock scheduling protocol, not a claim of force-cancelling real threads."""
    def __init__(self, *, on_wait=lambda _: None, timeouts=()):
        self.on_wait, self.timeouts, self.waits, self.futures = on_wait, set(timeouts), [], []
        self.drained = False
    def submit(self, function, provider, *args, **kwargs):
        try:
            future = InlineFuture(self, provider, value=function(provider, *args, **kwargs))
        except Exception as error:
            future = InlineFuture(self, provider, error=error)
        self.futures.append(future)
        return future
    def shutdown(self, *, wait, cancel_futures):
        self.drained = wait and cancel_futures


class SourceDeadlineTests(unittest.TestCase):
    setUp = ScheduleWorkflowTests.setUp
    def test_optional_sources_share_one_legacy_deadline_and_wait_only_remaining_time(self):
        elapsed = [100.0]
        def waited(provider):
            if provider == "osm":
                elapsed[0] += 12
        executor = InlineExecutor(on_wait=waited)
        original_search = self.google.search_places
        def search(parameters):
            elapsed[0] += 10
            return original_search(parameters)
        self.google.search_places = search
        with patch("app.unified_walks.monotonic", side_effect=lambda: elapsed[0]), patch(
                "app.unified_walks.ThreadPoolExecutor", return_value=executor):
            result = self.service.recommend({**self.form, "minutes": 60})
        self.assertEqual(self.osm.retrieve.call_args.kwargs["deadline"], 130)
        self.assertEqual(self.dgis.search.call_args.kwargs["deadline"], 130)
        self.assertEqual(executor.waits, [("osm", 20), ("dgis", 8)])
        self.assertTrue(executor.drained)
        self.assertEqual(result["diagnostics"]["searches"], {"osm": 1, "google": 1, "dgis": 1})

    def test_prepared_remaining_workflow_bounds_both_sources_before_invocation(self):
        form = {"query": self.form["query"], "minutes": 60}
        prepared = SimpleNamespace(form=form, resolved=self.service.resolver(form), intent_plan=None,
            deadline=112, intent_attempts=0, intent_status="deterministic", place_attempts=0)
        self.service.preparation = Mock()
        self.service.preparation.consume.return_value = prepared
        executor = InlineExecutor()
        with patch("app.unified_walks.monotonic", return_value=100), patch(
                "app.unified_walks.ThreadPoolExecutor", return_value=executor):
            self.service.recommend({**form, "consent": True})
        self.assertEqual(self.osm.retrieve.call_args.kwargs["deadline"], 109)
        self.assertEqual(self.dgis.search.call_args.kwargs["deadline"], 109)
        self.assertEqual(executor.waits, [("osm", 9), ("dgis", 9)])
        self.assertTrue(executor.drained)

    def test_expired_prepared_workflow_starts_no_sources_or_routes(self):
        form = {"query": self.form["query"], "minutes": 60}
        prepared = SimpleNamespace(form=form, resolved=self.service.resolver(form), intent_plan=None,
            deadline=102, intent_attempts=0, intent_status="deterministic", place_attempts=0)
        self.service.preparation = Mock()
        self.service.preparation.consume.return_value = prepared
        with patch("app.unified_walks.monotonic", return_value=100), self.assertRaises(GoogleError) as caught:
            self.service.recommend({**form, "consent": True})
        self.assertEqual(caught.exception.code, "context_timeout")
        self.osm.retrieve.assert_not_called()
        self.dgis.search.assert_not_called()
        self.assertEqual(self.google.search_count, 0)
        self.assertEqual(self.google.legs, [])

    def test_optional_timeout_is_unavailable_not_empty_success_and_workers_are_drained(self):
        executor = InlineExecutor(timeouts={"osm"})
        with patch("app.unified_walks.monotonic", return_value=100), patch(
                "app.unified_walks.ThreadPoolExecutor", return_value=executor):
            result = self.service.recommend({**self.form, "minutes": 60})
        self.assertFalse(result["search_complete"])
        self.assertEqual(result["source_receipt"]["osm"], {"status": "unavailable", "candidate_count": None,
            "selected_count": 0, "attempted": True, "error_code": "source_timeout"})
        self.assertTrue(executor.futures[0].cancelled)
        self.assertTrue(executor.drained)
        self.assertNotIn("PRIVATE_SOURCE_TIMEOUT", repr(result))
        self.assertEqual(result["resolved_context"]["source_availability"],
                         {"osm": "unavailable", "google": "completed", "dgis": "completed"})
        self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")


if __name__ == "__main__":
    unittest.main()
