"""Dataset experiment runner + regression gate (plan Task 21, Review Focus 6)."""

import json

import pytest

from scripts import run_experiment as rx


def _row(i, exp, got, stage="archived"):
    return {
        "input": {"id": f"s{i}", "filename": f"s{i}.txt"},
        "expected_output": {"expected_doc_class": exp, "expected_stage": "archived"},
        "output": {"doc_type": got, "stage": stage, "extracted_data": {}},
    }


def test_mock_experiment_scores_every_item(tmp_path, monkeypatch):
    items = [
        {"input": {"id": "a"}, "expected_output": {"expected_doc_class": "contract"}},
        {"input": {"id": "b"}, "expected_output": {"expected_doc_class": "correspondence"}},
    ]

    def task(*, item, **kw):
        return {"doc_type": item["expected_output"]["expected_doc_class"], "stage": "archived"}

    summary = rx.run_local(items, task)
    assert summary["n_items"] == 2 and summary["n_errors"] == 0
    assert summary["metrics"]["class_accuracy"] == 1.0
    assert summary["metrics"]["macro_class_accuracy"] == 1.0
    assert all(any(e.name == "class_correct" for e in r["evaluations"]) for r in summary["items"])


def test_task_error_counts_as_incorrect_and_is_isolated():
    items = [
        {"input": {"id": "a"}, "expected_output": {"expected_doc_class": "contract"}},
        {"input": {"id": "b"}, "expected_output": {"expected_doc_class": "contract"}},
    ]

    def task(*, item, **kw):
        if item["input"]["id"] == "a":
            raise RuntimeError("boom")
        return {"doc_type": "contract", "stage": "archived"}

    s = rx.run_local(items, task)
    assert s["n_errors"] == 1 and s["metrics"]["class_accuracy"] == 0.5


def test_regression_vs_baseline_exits_nonzero(tmp_path):
    baseline = {"n_items": 2, "n_errors": 0, "metrics": {"class_accuracy": 1.0, "macro_class_accuracy": 1.0}}
    worse = {"n_items": 2, "n_errors": 0, "metrics": {"class_accuracy": 0.5, "macro_class_accuracy": 0.5}}
    same = dict(baseline)
    assert rx.check_regression(same, baseline) == []
    problems = rx.check_regression(worse, baseline)
    assert problems and "class_accuracy" in problems[0]
    # small wobble inside tolerance is not a regression
    assert rx.check_regression({**same, "metrics": {"class_accuracy": 0.99, "macro_class_accuracy": 0.99}}, baseline, tolerance=0.02) == []


def test_empty_dataset_fails_closed():
    empty = {"n_items": 0, "n_errors": 0, "metrics": {}}
    baseline = {"n_items": 2, "n_errors": 0, "metrics": {"class_accuracy": 1.0}}
    assert rx.check_regression(empty, baseline)  # non-empty list → fail
    assert rx.check_regression(empty, None)  # even with no baseline


def test_task_errors_fail_the_gate_even_if_accuracy_holds():
    base = {"n_items": 2, "n_errors": 0, "metrics": {"class_accuracy": 1.0}}
    cur = {"n_items": 2, "n_errors": 1, "metrics": {"class_accuracy": 1.0}}
    assert any("error" in p for p in rx.check_regression(cur, base))


def test_missing_metric_in_current_fails():
    base = {"n_items": 1, "n_errors": 0, "metrics": {"class_accuracy": 1.0}}
    cur = {"n_items": 1, "n_errors": 0, "metrics": {}}
    assert rx.check_regression(cur, base)


def test_main_mock_end_to_end_and_baseline_gate(tmp_path, monkeypatch, capsys):
    out = tmp_path / "base.json"
    assert rx.main(["--dataset", "mailroom-fixtures", "--mock", "--max-items", "2", "--write-baseline", str(out)]) == 0
    base = json.loads(out.read_text())
    assert base["n_items"] == 2 and base["metrics"]["class_accuracy"] == 1.0
    assert rx.main(["--dataset", "mailroom-fixtures", "--mock", "--max-items", "2", "--baseline", str(out)]) == 0
    # hand-degrade the baseline's expectation upward is a no-op; degrade the *current* by demanding more items
    base["metrics"]["class_accuracy"] = 1.0
    base["n_items"] = 2
    degraded = tmp_path / "degraded.json"
    base["metrics"]["macro_class_accuracy"] = 5.0  # unattainable
    degraded.write_text(json.dumps(base))
    assert rx.main(["--dataset", "mailroom-fixtures", "--mock", "--max-items", "2", "--baseline", str(degraded)]) == 1


def test_unknown_dataset_fails_closed():
    assert rx.main(["--dataset", "does-not-exist", "--mock"]) != 0


def test_real_mode_refused_without_real_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock-key")
    assert rx.main(["--dataset", "mailroom-fixtures", "--real"]) != 0


def test_pilot_dataset_without_manifest_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(rx, "MANIFEST", tmp_path / "missing.csv")
    assert rx.main(["--dataset", "mailroom-pilot", "--mock"]) == 2


def test_fixture_dataset_excludes_ambiguous_and_covers_live_classes():
    rows = rx.fixture_rows(None)
    assert rows and not any(r["filename"].startswith("ambiguous") for r in rows)
    assert {r["expected_doc_class"] for r in rows} == set(rx.FIXTURE_CLASSES)
