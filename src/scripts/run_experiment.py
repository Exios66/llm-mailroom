#!/usr/bin/env python3
"""Run a Langfuse-style dataset experiment over the pilot manifest and gate it.

Each dataset item is pushed through the pipeline (``run_pilot.run_sample``),
scored by item evaluators (``class_correct``, ``stage_correct``, dojo
``extraction_overall_score``) and aggregated by run evaluators
(``class_accuracy``, ``macro_class_accuracy``, ``macro_f1``,
``stage_accuracy``). The gate compares the aggregates with an approved
baseline JSON and FAILS CLOSED: an empty dataset, task errors, or a missing
metric never pass.

Two execution paths share the same evaluators:

* default (offline, local): ``run_local`` — no Langfuse needed; this is what
  ``--mock`` and the CI gate use.
* ``--langfuse``: the hosted dataset through the SDK experiment runner
  (``Langfuse.get_dataset(...).run_experiment``), so the run appears under
  Experiments. Needs ``LANGFUSE_*`` credentials.

Usage:
    python scripts/run_experiment.py --dataset mailroom-fixtures --mock   # hermetic (CI gate)
    python scripts/run_experiment.py --dataset mailroom-pilot --mock     # needs docs/examples/samples (monorepo)
    python scripts/run_experiment.py --dataset mailroom-fixtures --mock --write-baseline docs/superpowers/baselines/experiment-baseline.json
    python scripts/run_experiment.py --dataset mailroom-fixtures --mock --baseline docs/superpowers/baselines/experiment-baseline.json
    python scripts/run_experiment.py --dataset mailroom-pilot --mock --langfuse
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from langfuse import Evaluation  # noqa: E402

MANIFEST = REPO_ROOT / "docs" / "examples" / "samples" / "manifest.csv"
GATED_METRICS = ("class_accuracy", "macro_class_accuracy", "macro_f1", "stage_accuracy")
DEFAULT_TOLERANCE = 0.02


def _get(obj: Any, key: str) -> Any:
    """Field access that works for local dict items and SDK DatasetItem objects."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# ── evaluators ──────────────────────────────────────────────────────────


def class_correct_evaluator(*, input, output, expected_output, **kwargs) -> Evaluation:
    from observability.classification_scoring import classes_match

    got = (output or {}).get("doc_type")
    want = (expected_output or {}).get("expected_doc_class")
    ok = bool(want) and classes_match(want, got)
    return Evaluation(name="class_correct", value=1 if ok else 0, data_type="BOOLEAN")


def stage_correct_evaluator(*, input, output, expected_output, **kwargs) -> Evaluation | None:
    want = (expected_output or {}).get("expected_stage")
    if not want:
        return None
    return Evaluation(
        name="stage_correct",
        value=1 if str((output or {}).get("stage") or "") == str(want) else 0,
        data_type="BOOLEAN",
    )


def extraction_overall_evaluator(*, input, output, expected_output, **kwargs) -> Evaluation | None:
    expected_fields = (expected_output or {}).get("expected_fields")
    doc_class = (expected_output or {}).get("expected_doc_class")
    predicted = (output or {}).get("extracted_data")
    if not expected_fields or not doc_class or not isinstance(predicted, dict):
        return None
    from observability.suite_scoring import is_unscorable, score_document_payload

    payload = score_document_payload(doc_class, predicted, expected_fields)
    if payload is None or is_unscorable(payload):
        return None
    overall = getattr(payload.get("extraction"), "overall_score", None)
    if not isinstance(overall, (int, float)):
        return None
    return Evaluation(name="extraction_overall_score", value=float(overall))


ITEM_EVALUATORS: list[Callable] = [
    class_correct_evaluator,
    stage_correct_evaluator,
    extraction_overall_evaluator,
]


# ── aggregation ─────────────────────────────────────────────────────────


def compute_metrics(rows: list[dict]) -> dict[str, float]:
    """Aggregate metrics from rows ``{expected, got, stage_ok}``.

    ``got`` is None for errored items (counted as incorrect for every class).
    """
    if not rows:
        return {}
    from observability.classification_scoring import classes_match

    n = len(rows)
    correct = [bool(r["expected"]) and classes_match(r["expected"], r["got"]) for r in rows]
    per_class: dict[str, list[bool]] = defaultdict(list)
    for r, ok in zip(rows, correct):
        per_class[r["expected"]].append(ok)
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    for r, ok in zip(rows, correct):
        if ok:
            tp[r["expected"]] += 1
        else:
            fn[r["expected"]] += 1
            if r["got"]:
                fp[str(r["got"])] += 1
    f1s = []
    for cls in set(per_class) | set(fp):
        p = tp[cls] / (tp[cls] + fp[cls]) if (tp[cls] + fp[cls]) else 0.0
        r_ = tp[cls] / (tp[cls] + fn[cls]) if (tp[cls] + fn[cls]) else 0.0
        f1s.append(2 * p * r_ / (p + r_) if (p + r_) else 0.0)
    metrics = {
        "class_accuracy": round(sum(correct) / n, 4),
        "macro_class_accuracy": round(sum(sum(v) / len(v) for v in per_class.values()) / len(per_class), 4),
        "macro_f1": round(sum(f1s) / len(f1s), 4),
    }
    staged = [r for r in rows if r.get("has_stage")]
    if staged:
        metrics["stage_accuracy"] = round(sum(1 for r in staged if r["stage_ok"]) / len(staged), 4)
    return metrics


def run_evaluators_from_rows(rows: list[dict]) -> list[Evaluation]:
    return [Evaluation(name=k, value=v) for k, v in compute_metrics(rows).items()]


# ── execution ───────────────────────────────────────────────────────────


def _row_for(item: Any, output: dict | None) -> dict:
    want = _get(item, "expected_output") or {}
    expected_stage = want.get("expected_stage")
    return {
        "expected": want.get("expected_doc_class"),
        "got": (output or {}).get("doc_type"),
        "has_stage": bool(expected_stage),
        "stage_ok": bool(output) and bool(expected_stage) and str(output.get("stage") or "") == str(expected_stage),
    }


def run_local(items: list[dict], task: Callable, evaluators: list[Callable] | None = None) -> dict:
    """Offline experiment loop with the SDK's evaluator contract and error isolation."""
    evaluators = ITEM_EVALUATORS if evaluators is None else evaluators
    results, rows, n_errors = [], [], 0
    for item in items:
        output, error = None, None
        try:
            output = task(item=item)
        except Exception as exc:  # error isolation: one bad item never stops the run
            n_errors += 1
            error = f"{type(exc).__name__}: {exc}"
        evals: list[Evaluation] = []
        if output is not None:
            for ev in evaluators:
                res = ev(
                    input=_get(item, "input"),
                    output=output,
                    expected_output=_get(item, "expected_output"),
                    metadata=_get(item, "metadata"),
                )
                if res is not None:
                    evals.extend(res if isinstance(res, list) else [res])
        else:
            evals.append(Evaluation(name="class_correct", value=0, data_type="BOOLEAN"))
        rows.append(_row_for(item, output))
        results.append({"input": _get(item, "input"), "error": error, "evaluations": evals})
    return {
        "n_items": len(items),
        "n_errors": n_errors,
        "metrics": compute_metrics(rows),
        "items": results,
    }


def check_regression(current: dict, baseline: dict | None, tolerance: float = DEFAULT_TOLERANCE) -> list[str]:
    """Return a list of gate failures; an empty list means the gate passes."""
    problems: list[str] = []
    if not current.get("n_items"):
        problems.append("empty dataset: zero items were evaluated (fail closed)")
    if current.get("n_errors"):
        problems.append(f"{current['n_errors']} item(s) raised task errors")
    if baseline:
        cur = current.get("metrics") or {}
        for name, base_value in (baseline.get("metrics") or {}).items():
            if name not in cur:
                problems.append(f"metric {name} missing from the current run")
            elif cur[name] < base_value - tolerance:
                problems.append(f"{name} regressed: {cur[name]} < baseline {base_value} (tolerance {tolerance})")
    return problems


# ── datasets ────────────────────────────────────────────────────────────


FIXTURES_DATASET = "mailroom-fixtures"
FIXTURES_DIR = SRC_DIR / "tests" / "fixtures"
FIXTURE_CLASSES = ("contract", "corporate_record", "correspondence", "insurance_claim")


def fixture_rows(max_items: int | None) -> list[dict]:
    """Hermetic dataset: the committed plain-text test fixtures, class = directory.

    Needs no pilot PDFs or network — the dataset the CI gate runs on.
    Ambiguous fixtures are excluded (they have no single true class).
    """
    rows = []
    for cls in FIXTURE_CLASSES:
        for path in sorted((FIXTURES_DIR / cls).glob("*.txt")):
            if path.stem.startswith("ambiguous"):
                continue
            rows.append(
                {
                    "id": f"{cls}-{path.stem}",
                    "subdir": "fixtures",
                    "filename": path.name,
                    "expected_doc_class": cls,
                    # No stage truth: mock extraction legitimately routes some fixtures to review.
                    "expected_stage": "",
                    "size_tier": "small",
                    "dataset": "fixtures",
                    "_path": str(path),
                }
            )
    return rows[:max_items] if max_items else rows


def stage_fixture_samples(rows: list[dict]) -> None:
    """Copy fixture files to where ``run_pilot.run_sample`` reads samples from."""
    import shutil

    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "samples" / "fixtures"
    base.mkdir(parents=True, exist_ok=True)
    for row in rows:
        shutil.copyfile(row["_path"], base / row["filename"])


def manifest_rows(dataset_name: str, max_items: int | None, *, mock: bool) -> list[dict] | None:
    """Rows for ``dataset_name`` or None when the dataset is unknown/unavailable."""
    if dataset_name == FIXTURES_DATASET:
        return fixture_rows(max_items)
    if not MANIFEST.exists():  # the pilot sample set is not part of this repo checkout
        return None
    from scripts.sync_dataset import SOURCE_DATASETS

    keys = [k for k, v in SOURCE_DATASETS.items() if v == dataset_name]
    if not keys:
        return None
    with MANIFEST.open() as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("dataset") or "original") == keys[0]]
    if not mock:
        from scripts.prepare_samples import is_real_sample

        rows = [r for r in rows if is_real_sample(r)]
    return rows[:max_items] if max_items else rows


def build_items(rows: list[dict]) -> list[dict]:
    items = []
    for row in rows:
        expected: dict[str, Any] = {
            "expected_doc_class": row["expected_doc_class"],
            "expected_stage": row["expected_stage"],
        }
        raw = (row.get("expected_fields") or "").strip()
        if raw:
            try:
                expected["expected_fields"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        items.append({"input": dict(row), "expected_output": expected, "metadata": {"sample_id": row["id"]}})
    return items


def make_task(mock: bool, run_id: str) -> Callable:
    def task(*, item, **kwargs):
        from scripts import run_pilot

        sample = _get(item, "input")
        if not isinstance(sample, dict) or "subdir" not in sample:
            raise ValueError("dataset item input is not a manifest sample row")
        row = run_pilot.run_sample(sample, mock, session_id=f"experiment-{run_id}", run_id=run_id)
        return {
            "doc_type": row.get("doc_type"),
            "stage": row.get("stage"),
            "extracted_data": row.get("extracted_data"),
        }

    return task


def run_langfuse(dataset_name: str, items: list[dict], task: Callable, run_name: str) -> dict:
    """Hosted run through the SDK experiment runner (shows under Experiments)."""
    from langfuse import get_client

    client = get_client()
    result = client.run_experiment(
        name=f"{dataset_name}-experiment",
        run_name=run_name,
        description="llm-mailroom pilot-manifest experiment",
        data=items,
        task=task,
        evaluators=ITEM_EVALUATORS,
        run_evaluators=[
            lambda *, item_results, **kw: run_evaluators_from_rows(
                [_row_for(r.item, r.output) for r in item_results]
            )
        ],
    )
    client.flush()
    rows = [_row_for(r.item, r.output) for r in result.item_results]
    return {
        "n_items": len(items),
        "n_errors": len(items) - len(result.item_results),
        "metrics": compute_metrics(rows),
        "items": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and gate a pilot dataset experiment.")
    parser.add_argument("--dataset", required=True, help="Dataset name, e.g. mailroom-pilot.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="Deterministic fake LLM (no API key).")
    mode.add_argument("--real", action="store_true", help="Real LLM (needs OPENROUTER_API_KEY).")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--baseline", help="Approved baseline JSON to gate against.")
    parser.add_argument("--write-baseline", help="Write this run's summary as a baseline JSON.")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--langfuse", action="store_true", help="Run via the hosted SDK experiment runner.")
    args = parser.parse_args(argv)

    if args.real:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key or key == "mock-key":
            print("refusing --real: OPENROUTER_API_KEY is not a real key", file=sys.stderr)
            return 2
    elif not args.langfuse:
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        os.environ["OBSERVABILITY_ENVIRONMENT"] = "mock"
        os.environ.setdefault("MAILROOM_RELATIONS_EMBEDDINGS", "0")  # hermetic: no embedding calls

    rows = manifest_rows(args.dataset, args.max_items, mock=args.mock)
    if rows is None:
        print(f"unknown dataset: {args.dataset}", file=sys.stderr)
        return 2

    if args.dataset == FIXTURES_DATASET:
        stage_fixture_samples(rows)
    else:
        from scripts.prepare_samples import prepare_samples

        prepare_samples()
    items = build_items(rows)
    run_id = datetime.now(timezone.utc).isoformat()
    task = make_task(args.mock, run_id)
    if args.langfuse:
        summary = run_langfuse(args.dataset, items, task, f"{args.dataset}-{run_id}")
    else:
        summary = run_local(items, task)

    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    problems = check_regression(summary, baseline, args.tolerance)
    public = {
        "dataset": args.dataset,
        "mode": "mock" if args.mock else "real",
        "n_items": summary["n_items"],
        "n_errors": summary["n_errors"],
        "metrics": summary["metrics"],
    }
    print(json.dumps(public, indent=2, sort_keys=True))
    if args.write_baseline:
        Path(args.write_baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write_baseline).write_text(json.dumps(public, indent=2, sort_keys=True) + "\n")
    for p in problems:
        print(f"GATE FAIL: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
