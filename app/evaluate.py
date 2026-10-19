"""Reproducible task-1 scenario evaluation. No live LLM unless --llm is passed.

python -m app.evaluate --output tmp/backend-evaluation.json --run-tests
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import unittest

from .generation import recommend
from .retrieval import ROOT, Retriever, architecture_evidence, feature


SCENARIOS = [
    {"name": "30–60 минут возле MEGA", "query": "У меня 45 минут возле Mega, хочу короткую прогулку", "start": {"label": "MEGA Silk Way", "lat": 51.0892, "lon": 71.4074}, "minutes": 45, "time_of_day": "day", "needs_results": True},
    {"name": "Тихий кофе и розетка", "query": "Хочу тихий кофе с розеткой", "start": {"label": "MEGA Silk Way", "lat": 51.0892, "lon": 71.4074}, "minutes": 60, "time_of_day": "day", "needs_results": False},
    {"name": "Прогулка с ребёнком", "query": "Погулять с ребёнком на свежем воздухе", "start": {"label": "Ботанический сад", "lat": 51.1053, "lon": 71.4121}, "minutes": 60, "time_of_day": "day", "needs_results": True},
    {"name": "Два часа у туриста", "query": "Я турист, хочу познакомиться с архитектурой Астаны", "start": {"label": "Бәйтерек", "lat": 51.1283, "lon": 71.4305}, "minutes": 120, "time_of_day": "day", "needs_results": True},
    {"name": "Ночная активность", "query": "Ночью хочу найти активность рядом", "start": {"label": "Бәйтерек", "lat": 51.1283, "lon": 71.4305}, "minutes": 60, "time_of_day": "night", "needs_results": False},
]


def evaluate(retriever: Retriever, *, use_llm: bool = False) -> dict:
    by_id = {poi["id"]: poi for poi in retriever.places}
    results = []
    for scenario in SCENARIOS:
        response = recommend(retriever, scenario["query"], scenario["minutes"], scenario["start"], scenario["time_of_day"], use_llm=use_llm)
        selected = response["recommendations"]
        ids = [item["id"] for item in selected]
        intent = response["diagnostics"]["intent"]
        constraints = all(
            (not intent["socket"] or feature(by_id[item["id"]], "socket") == "yes")
            and (not intent["accessible"] or feature(by_id[item["id"]], "wheelchair") == "yes")
            and (not intent["night"] or by_id[item["id"]].get("opening_hours") == "24/7")
            and (not intent["child"] or item["category"] != "bar")
            and (not intent["architecture"] or item["category"] in intent["preferred_categories"] or architecture_evidence(by_id[item["id"]]))
            for item in selected
        )
        checks = {
            "budget": 0 <= response["plan"]["total_minutes"] <= scenario["minutes"],
            "limit": 0 <= len(selected) <= 3 and len(set(ids)) == len(ids),
            "sources": all(identifier in by_id for identifier in ids) and set(ids) == {source["id"] for source in response["sources"]},
            "physical_deduplication": all(identifier not in retriever.duplicate_of for identifier in ids),
            "constraints": constraints,
            "useful_result_or_explained_abstention": bool(selected) if scenario["needs_results"] else bool(selected or response.get("clarification")),
        }
        results.append({
            "name": scenario["name"], "query": scenario["query"], "start": scenario["start"],
            "minutes": scenario["minutes"], "result_count": len(selected),
            "total_minutes": response["plan"]["total_minutes"], "mode": response["mode"],
            "pass": all(checks.values()), "checks": checks,
            "outcome": "recommendations" if selected else "explained_abstention",
            "clarification": response.get("clarification"),
            "poi_ids": ids, "warnings": response["warnings"],
            "llm_status": response["diagnostics"]["llm_status"],
            "llm_attempts": response["diagnostics"]["llm_attempts"],
            "llm_rejections": response["diagnostics"]["llm_rejections"],
        })
    data_path = None
    if retriever.data_path:
        data_path = retriever.data_path.relative_to(ROOT).as_posix() if retriever.data_path.is_relative_to(ROOT) else retriever.data_path.name
    return {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": {"name": retriever.dataset.get("name"), "is_demo": retriever.dataset.get("is_demo", False), "places_count": len(retriever.places), "path": data_path, "sha256": retriever.data_sha256},
        "live_llm_requested": use_llm,
        "live_llm_used": any(item["mode"].startswith("llm-grounded") for item in results),
        "scenarios": results,
        "summary": {"scenarios_total": len(results), "scenarios_passed": sum(item["pass"] for item in results), "tests": None, "llm": {"eligible_scenarios": sum(item["result_count"] > 0 for item in results), "validated_scenarios": sum(item["mode"].startswith("llm-grounded") for item in results), "fallback_scenarios": sum(item["result_count"] > 0 and not item["mode"].startswith("llm-grounded") for item in results), "total_attempts": sum(item["llm_attempts"] for item in results)}},
        "limitations": ["Проверяются сценарные ограничения и источники, а не полное качество городских рекомендаций.", "Пустой ответ при неподтверждённой розетке или режиме 24/7 — честное воздержание.", "Нет полевой проверки POI, дорожного графа и гарантии семантической достоверности свободного текста."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--llm", action="store_true", help="Actually call the configured provider (may incur cost)")
    args = parser.parse_args()
    report = evaluate(Retriever(args.data), use_llm=args.llm)
    if args.run_tests:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=1).run(suite)
        unsuccessful = {getattr(test, "test_case", test).id() for test, _ in result.failures + result.errors + result.skipped}
        report["summary"]["tests"] = {"run": result.testsRun, "passed": max(0, result.testsRun - len(unsuccessful)), "failed": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "success": result.wasSuccessful(), "failure_tests": [test.id() for test, _ in result.failures], "error_tests": [test.id() for test, _ in result.errors]}
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
        print(f"Evaluation saved: {args.output}; scenarios {report['summary']['scenarios_passed']}/{len(SCENARIOS)}")
    else:
        print(serialized)
    tests_ok = report["summary"]["tests"] is None or report["summary"]["tests"]["success"]
    return 0 if report["summary"]["scenarios_passed"] == len(SCENARIOS) and tests_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
