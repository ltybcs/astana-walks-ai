"""Synthetic provider failure isolation; no network, credentials or inference."""
from copy import deepcopy
from unittest.mock import Mock
import unittest
from app.google_budget import GoogleError
from app.unified_walks import _stop
import test_unified_walks as fixtures
poi = fixtures.poi


class PartialSourceTests(unittest.TestCase):
    setUp = fixtures.UnifiedTests.setUp

    def test_each_optional_source_failure_discards_its_records_without_forging_empty_success(self):
        for failed in ("osm", "dgis", "both"):
            with self.subTest(failed=failed):
                self.osm.retrieve.side_effect = RuntimeError("PRIVATE") if failed in {"osm", "both"} else None
                self.dgis.search.side_effect = RuntimeError("PRIVATE") if failed in {"dgis", "both"} else None
                result=self.service.recommend(self.form)
                expected={"osm", "dgis"} if failed=="both" else {failed}
                self.assertFalse(result["search_complete"])
                for source in expected:
                    self.assertEqual(result["source_receipt"][source]["status"], "unavailable")
                    self.assertIsNone(result["source_receipt"][source]["candidate_count"])
                    self.assertEqual(result["sources"][source], [])
                    self.assertNotIn(source, {s["provider"] for o in result["alternatives"] for s in o["stops"]})
                self.assertNotIn("PRIVATE", repr(result))
                self.assertEqual(len(result["grounding"]["sources"]), 1)

    def test_invalid_optional_shapes_cannot_publish_a_partial_source_snapshot(self):
        for source, bad in (("osm", {"selected":[{}]}), ("osm", {"selected":[{"poi":{"lat":True}}]}),
                            ("dgis", {"places":[{"name":"PRIVATE"}]}), ("dgis", {"places":"PRIVATE"})):
            with self.subTest(source=source):
                old=deepcopy(self.osm.retrieve.return_value if source=="osm" else self.dgis.search.return_value)
                target=self.osm.retrieve if source=="osm" else self.dgis.search
                target.return_value=bad
                result=self.service.recommend(self.form)
                self.assertEqual(result["source_receipt"][source]["status"], "unavailable")
                self.assertEqual(result["sources"][source], [])
                self.assertNotIn("PRIVATE", repr(result))
                target.return_value=old

    def test_missing_dgis_or_throwing_status_never_calls_its_search(self):
        for absent in (True, False):
            self.service.dgis=None if absent else self.dgis
            self.dgis.status.side_effect=RuntimeError("PRIVATE")
            result=self.service.recommend(self.form)
            self.assertFalse(result["source_receipt"]["dgis"]["attempted"])
            self.assertEqual(result["diagnostics"]["searches"]["dgis"],0)
            self.dgis.search.assert_not_called()

    def test_google_search_and_invalid_grounding_still_fail_without_narration(self):
        for response in (None, {"summary":"", "places":[]}):
            self.narrator.reset_mock()
            self.google.search_places=Mock(side_effect=GoogleError("safe", "unavailable",502)) if response is None else Mock(return_value=response)
            with self.assertRaises(GoogleError):self.service.recommend(self.form)
            self.narrator.assert_not_called()

    def test_google_exhaustion_stops_before_all_sources(self):
        self.google.budget.status.return_value={"state":"exhausted"}
        with self.assertRaises(GoogleError):self.service.recommend(self.form)
        self.osm.retrieve.assert_not_called();self.dgis.search.assert_not_called();self.narrator.assert_not_called()

    def test_own_property_provenance_never_invents_observation_date(self):
        place=poi("osm:node/1","Synthetic",51.09,71.41)
        place.update(features={"socket":"yes","quiet":"no","wheelchair":"unknown"},collected_at="2026-10-20T12:00:00Z")
        before=deepcopy(place)
        stop=_stop(place,"osm","o0")
        self.assertEqual(place,before)
        self.assertEqual(stop["source_observed_at"],place["collected_at"])
        self.assertEqual([(e["property"],e["value"]) for e in stop["feature_evidence"]],[("socket","yes"),("quiet","no"),("wheelchair","unknown")])
        self.assertTrue(all(e["place_id"]==place["id"] and e["source_url"]==place["source_url"] for e in stop["feature_evidence"]))
        place.pop("collected_at")
        self.assertIsNone(_stop(place,"osm","o0")["source_observed_at"])
        self.assertEqual(_stop(place,"osm","o0")["feature_evidence"],[])
        place["collected_at"]="PRIVATE_INVALID"
        self.assertIsNone(_stop(place,"osm","o0")["source_observed_at"])


if __name__=="__main__":unittest.main()
