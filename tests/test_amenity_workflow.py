"""Convenience alternatives retain type, safety, route and source identity gates."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from time import monotonic
import unittest
from unittest.mock import Mock

from app.lab_context import resolve_request, PUBLIC_ORIGINS
from app.retrieval import Retriever
from app.semantic_intent import deterministic_plan
from app.unified_walks import UnifiedWalks
from tests.test_unified_walks import Google, poi


class AmenityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.google = Google()
        self.dgis = Mock()
        self.dgis.status.return_value = {"available": True}
        self.dgis.search.return_value = {"places": [], "fallback_places": [], "warnings": []}
        self.narrator = Mock(return_value=(None, "local_model_unconfigured"))
        self.form = {"query": "Хочу кафе с розеткой возле Mega на 45 минут", "time_of_day": "day", "consent": True}
        self.near = poi("osm:node/1", "Synthetic nearby cafe", 51.0893, 71.4075, "cafe")

    def service(self, places):
        retriever = Retriever(data={"dataset": {"is_demo": True}, "places": deepcopy(places)})
        service = UnifiedWalks(resolve_request, retriever, self.google, self.dgis, narrator=self.narrator)
        service.osm.retrieve = Mock(wraps=retriever.retrieve)
        return service

    def test_unknown_socket_ready_with_honest_note_and_one_external_search_each(self):
        before = deepcopy(self.near)
        service = self.service([self.near])
        result = service.recommend(self.form)
        option = result["alternatives"][0]
        self.assertTrue(option["route_built"])
        self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["socket"])
        self.assertIn("подходящие по остальным условиям", option["amenity_fallback"]["notice"])
        stop = option["stops"][0]
        self.assertEqual(stop["unconfirmed_amenities"], ["socket"])
        self.assertEqual(stop["requested_features"], ["socket"])
        self.assertIn("Не подтверждены", stop["amenity_note"])
        self.assertNotEqual(stop["features"].get("socket"), "yes")
        self.assertEqual(self.near, before)
        self.assertEqual(self.google.search_count, 1)
        self.assertEqual(self.dgis.search.call_count, 1)
        self.assertEqual(service.osm.retrieve.call_count, 2)  # Same in-memory corpus, no second HTTP.
        self.assertEqual(result["diagnostics"]["searches"], {"osm": 1, "google": 1, "dgis": 1})
        self.assertEqual(len(self.google.legs), 2)
        self.assertEqual(result["sources"]["osm"][0]["source_id"], stop["source_id"])
        self.assertEqual(result["source_receipt"]["osm"], {"status": "searched", "candidate_count": 1,
            "strict_candidate_count": 0, "alternative_candidate_count": 1, "selected_count": 1})
        self.assertEqual(result["grounding"]["summary"], "Полный синтетический ответ Google [0].")

    def test_confirmed_socket_wins_without_alternative_note_or_extra_routing(self):
        confirmed = {**self.near, "features": {"socket": "yes"}}
        result = self.service([confirmed]).recommend(self.form)
        option = result["alternatives"][0]
        self.assertTrue(option["route_built"])
        self.assertNotIn("amenity_fallback", option)
        self.assertNotIn("unconfirmed_amenities", option["stops"][0])
        self.assertEqual(len(self.google.legs), 2)
        self.assertEqual(result["source_receipt"]["osm"]["strict_candidate_count"], 1)

    def test_wifi_unknown_socket_yes_labels_only_wifi(self):
        self.form["query"] = "Хочу кафе с розеткой и Wi-Fi возле Mega на 45 минут"
        result = self.service([{**self.near, "features": {"socket": "yes"}}]).recommend(self.form)
        option = result["alternatives"][0]
        self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["wifi"])
        self.assertEqual(option["stops"][0]["unconfirmed_amenities"], ["wifi"])
        self.assertIn("Wi-Fi", option["stops"][0]["amenity_note"])
        self.assertEqual(option["stops"][0]["requested_features"], ["socket", "wifi"])

    def test_known_no_wrong_type_accessibility_private_child_and_hours_not_relaxed(self):
        cases = [
            (self.form["query"], {**self.near, "features": {"socket": "no"}}),
            (self.form["query"], {**self.near, "category": "pharmacy"}),
            ("Хочу кафе с розеткой для коляски возле Mega на 45 минут", {**self.near, "features": {"wheelchair": "no"}}),
            (self.form["query"], {**self.near, "source_tags": {"access": "private"}}),
            ("Хочу кафе с розеткой с ребёнком возле Mega на 45 минут", {**self.near, "source_categories": ["cafe", "bar"]}),
            ("Хочу кафе с розеткой возле Mega на 45 минут, обязательно 24/7", self.near),
            ("Хочу аптеку с розеткой возле Mega на 45 минут", self.near),
        ]
        for query, place in cases:
            with self.subTest(query=query, place=place):
                result = self.service([place]).recommend({**self.form, "query": query})
                self.assertFalse(result["alternatives"][0]["route_built"])
                self.assertNotIn("amenity_fallback", result["alternatives"][0])

    def test_unverified_or_infeasible_routes_never_become_convenience_fallback(self):
        for duration in (None, "7200s"):
            with self.subTest(duration=duration):
                service = self.service([self.near])
                self.google.fail_leg = duration is None
                if duration:
                    self.google.compute_route = Mock(return_value={"routes": [{"duration": duration, "distanceMeters": 5000,
                        "attribution": {"title": "Google Maps", "url": "https://www.google.com/maps/"}}]})
                result = service.recommend(self.form)
                self.assertFalse(result["alternatives"][0]["route_built"])
                self.assertNotIn("amenity_fallback", result["alternatives"][0])
                self.narrator.assert_not_called()

    def test_each_budget_prefers_strict_and_route_ids_are_unique_across_pools(self):
        far = {**self.near, "id": "osm:node/2", "name": "Synthetic confirmed cafe", "lat": 51.094,
               "features": {"socket": "yes"}}
        service = self.service([far, self.near])
        resolved = resolve_request({key:value for key,value in self.form.items() if key != "consent"})
        resolved["resolved_context"]["budget_options"] = [45, 120]
        service.resolver = lambda _: deepcopy(resolved)
        calls = []
        def route(start, finish):
            calls.append((deepcopy(start), deepcopy(finish)))
            points = [part.get("latLng", {}).get("latitude") for part in (start, finish)]
            return {"routes": [{"duration": "1800s" if far["lat"] in points else "120s", "distanceMeters": 120,
                "attribution": {"title": "Google Maps", "url": "https://www.google.com/maps/"}}]}
        self.google.compute_route = route
        result = service.recommend(self.form)
        small, large = result["alternatives"]
        self.assertEqual(small["amenity_fallback"]["unconfirmed"], ["socket"])
        self.assertEqual(small["stops"][0]["id"], self.near["id"])
        self.assertNotIn("amenity_fallback", large)
        self.assertEqual(large["stops"][0]["id"], far["id"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({repr(call) for call in calls}), 4)
        references = {item["source_id"]: item["id"] for item in result["sources"]["osm"]}
        self.assertEqual(len(references), 2)
        for option in result["alternatives"]:
            for stop in option["stops"]:
                self.assertEqual(references[stop["source_id"]], stop["id"])

    def test_failed_or_conflicting_source_cannot_leak_alternates(self):
        for mode in ("malformed", "conflicting"):
            with self.subTest(mode=mode):
                service = self.service([])
                if mode == "malformed":
                    self.dgis.search.return_value = {"places": [], "fallback_places": [None]}
                else:
                    self.dgis.search.return_value = {"places": [self.near], "fallback_places": [{**self.near, "lat": 51.094}]}
                result = service.recommend(self.form)
                self.assertEqual(result["source_receipt"]["dgis"]["status"], "unavailable")
                self.assertEqual(result["sources"]["dgis"], [])
                self.assertFalse(result["alternatives"][0]["route_built"])
        self.assertEqual(len(self.google.legs), 0)

    def test_fallback_cannot_extend_visit_beyond_reported_opening_interval(self):
        now = datetime(2026, 10, 19, 5, tzinfo=timezone.utc)  # Monday10:00 Astana.
        for hours, ready in (("Mo 09:00-10:10", False), ("Mo 09:00-18:00", True)):
            with self.subTest(hours=hours):
                service = self.service([{**self.near, "opening_hours": hours}])
                service.clock = lambda: now
                service.resolver = lambda form: resolve_request(form, now=now)
                result = service.recommend({key:value for key,value in self.form.items() if key != "time_of_day"})
                option = result["alternatives"][0]
                self.assertEqual(option["route_built"], ready)
                self.assertEqual("amenity_fallback" in option, ready)
                if ready:
                    self.assertEqual(option["stops"][0]["opening_status"]["status"], "open")

    def test_shared_route_memo_never_refetches_identical_directed_legs(self):
        from app.unified_walks import _stop
        service = self.service([])
        origin = PUBLIC_ORIGINS["mega"]
        stop = _stop(self.near, "osm", "o0")
        memo = {}
        first = service._plans([stop], [45, 120], {"activity_mode": "single_venue"}, origin, route_memo=memo)
        second = service._plans([stop], [45], {"activity_mode": "single_venue"}, origin, route_memo=memo)
        self.assertTrue(all(option["route_built"] for option in first + second))
        self.assertEqual(len(self.google.legs), 2)
        self.assertEqual(len(memo), 2)

    def test_tag_only_architecture_proof_survives_projection_and_fallback(self):
        query = "Хочу архитектуру с Wi-Fi возле Mega на 45 минут"
        source = {**self.near, "category": "museum", "source_tags": {"historic": "building", "private_extra": "UNFORWARDED"}}
        service = self.service([source])
        plan = deterministic_plan(query)
        form = {"query": query, "time_of_day": "day"}
        resolved = resolve_request(form)
        service.preparation = Mock()
        service.preparation.consume.return_value = SimpleNamespace(form=form, resolved=resolved, intent_plan=plan,
            deadline=monotonic()+120, intent_attempts=0, intent_status="synthetic", place_attempts=0)
        result = service.recommend({**form, "consent": True})
        option = result["alternatives"][0]
        self.assertTrue(option["route_built"])
        self.assertEqual(option["amenity_fallback"]["unconfirmed"], ["wifi"])
        self.assertEqual(option["stops"][0]["source_tags"], {"historic": "building"})
        self.assertEqual(option["stops"][0]["requested_features"], ["wifi"])

    def test_unrequested_unknowns_are_not_projected_as_requested_facts(self):
        for query, expected in (("Хочу кафе возле Mega с вайфаем на 45 минут", ["wifi"]),
                                ("Хочу кафе возле Mega на 45 минут", [])):
            with self.subTest(query=query):
                result = self.service([self.near]).recommend({**self.form, "query": query})
                stop = result["alternatives"][0]["stops"][0]
                self.assertEqual(stop["requested_features"], expected)
                self.assertNotEqual(stop["features"].get("quiet"), "yes")

    def test_narration_receives_relaxed_copy_only_after_verified_fallback(self):
        service = self.service([self.near])
        plan = deterministic_plan(self.form["query"])
        original = plan.as_dict()
        resolved = resolve_request({key:value for key,value in self.form.items() if key != "consent"})
        prepared = SimpleNamespace(form={key:value for key,value in self.form.items() if key != "consent"}, resolved=resolved, intent_plan=plan, deadline=monotonic()+120,
                                   intent_attempts=1, intent_status="validated", place_attempts=0)
        service.preparation = Mock()
        service.preparation.consume.return_value = prepared
        service._custom_narrator = False
        service.gateway = Mock()
        from contextlib import nullcontext
        service.gateway.session.return_value = nullcontext()
        result = service.recommend(self.form)
        self.assertTrue(result["alternatives"][0]["route_built"])
        self.assertEqual(plan.as_dict(), original)
        received = self.narrator.call_args.kwargs["intent_plan"]
        self.assertIsNot(received, plan)
        self.assertFalse(next(item for item in received.requirements if item.key == "socket").required)
        self.assertEqual(self.narrator.call_count, 1)


if __name__ == '__main__':
    unittest.main()
