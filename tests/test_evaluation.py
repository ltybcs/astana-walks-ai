import unittest

from app.evaluate import evaluate
from app.retrieval import Retriever


class ScenarioEvaluationTests(unittest.TestCase):
    def test_five_required_scenarios_and_honest_abstention(self):
        report = evaluate(Retriever(), use_llm=False)
        self.assertEqual(report["summary"]["scenarios_total"], 5)
        self.assertEqual(report["summary"]["scenarios_passed"], 5)
        self.assertFalse(report["live_llm_used"])
        self.assertEqual(report["dataset"]["path"], "data/poi.sample.json")
        self.assertEqual(len(report["dataset"]["sha256"]), 64)
        self.assertEqual(report["scenarios"][1]["outcome"], "explained_abstention")
        self.assertEqual(report["scenarios"][4]["outcome"], "explained_abstention")
