#!/usr/bin/env python3
"""Run real mailroom-dataset documents through the pipeline and record flow.

The manifest is a pulled (pinned) master-list of real documents. This runner
fetches the real ``doc_text`` for selected rows, verifies the recorded sha256,
runs the pipeline, and writes:

- one run journal per document (``<base>/diagnostics/runs/*.json``) with the
  exact LangGraph node ingress order and terminal outcome;
- one experiment summary (validity checks, paths, revisions) that the
  diagnostics API/React viewer can load;
- an optional replay comparison against a prior journal.

Default mode is deterministic mock LLM so routing and runtime behavior are
testable without paid calls. ``--real`` uses the real provider.

    PYTHONPATH=src python src/scripts/smoke_live_documents.py --live --per-class 1
    PYTHONPATH=src python src/scripts/smoke_live_documents.py --manifest \\
        src/tests/fixtures/live_manifest_sample.csv --per-class 1 --mock
    PYTHONPATH=src python src/scripts/smoke_live_documents.py --replay \\
        data/live_smoke/<stamp>/runs/<journal>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.live_manifest import (  # noqa: E402
    CLASSES,
    DATASET_ID,
    DEFAULT_MAX_CHARS,
    build_manifest_rows,
    fetch_texts,
    load_frames,
    load_manifest,
    resolve_revision,
    select_smoke_rows,
)

FIXTURE = SRC_DIR / "tests" / "fixtures" / "live_manifest_sample.csv"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe(value: str, limit: int = 60) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value)
    return cleaned[:limit] or "doc"


def _load(args: argparse.Namespace, *, select: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    if args.live:
        revision = resolve_revision(args.revision)
        gt, blind, provenance = load_frames(args.revision)
        rows, stats = build_manifest_rows(gt, blind, revision)
        selected = (
            select_smoke_rows(
                rows,
                per_class=args.per_class,
                classes=tuple(args.classes),
                max_chars=args.max_chars,
            )
            if select
            else rows
        )
        if stats["integrity"]["mismatched"]:
            raise SystemExit("refusing: content_sha256 mismatches in the live corpus")
        return selected, revision, "live"
    manifest = Path(args.manifest or FIXTURE)
    rows = load_manifest(manifest)
    revision = resolve_revision(rows[0].get("hub_revision") or rows[0].get("hub_sha"))
    selected = (
        select_smoke_rows(
            rows,
            per_class=args.per_class,
            classes=tuple(args.classes),
            max_chars=args.max_chars,
        )
        if select
        else rows
    )
    return selected, revision, str(manifest)


def _mock_expect(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = json.loads(row["expected_fields"] or "{}")
    fields.pop("confidence", None)
    doc_class = row["expected_doc_class"]
    classification: dict[str, Any] = {
        "doc_type": doc_class,
        "confidence": 0.99,
        "reasoning": "smoke-mock",
    }
    if doc_class == "contract":
        classification["contract_subtype"] = row.get("sorter_subclass") or "other"
    else:
        classification["doc_subclass"] = row.get("sorter_subclass") or None
    return classification, fields


def _mock_client(agent_name: str, classification: dict, extraction: dict) -> MagicMock:
    def create(**kwargs: Any) -> MagicMock:
        content = "mock free-form output"
        if agent_name.endswith("_specialist") or "specialist" in agent_name:
            content = json.dumps({**extraction, "confidence": 0.99})
        elif "sorter" in agent_name:
            content = json.dumps(classification)
        elif agent_name == "boss":
            content = json.dumps({"decision": "approved", "reasoning": "mock", "resolution_notes": ""})
        elif "judge" in agent_name:
            content = json.dumps({"verdict": "complete", "findings": [], "confidence": 0.99})
        elif agent_name == "arbiter":
            content = json.dumps(
                {"decision": "accept_with_caveats", "reasoning": "mock", "fix_list": []}
            )
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = content
        return response

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


def _run_one(
    row: dict[str, Any],
    text: str,
    *,
    base: Path,
    mode: str,
    revision: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from graph.build_graph import run_pipeline
    from graph.run_events import RunEventRecorder
    from pipeline.bins import ensure_dirs, inbox_dir

    ensure_dirs(inbox_dir())
    local = inbox_dir() / f"{_safe(row['id'])}.txt"
    local.write_text(text, encoding="utf-8")
    expected_fields = json.loads(row["expected_fields"] or "{}")
    ground_truth = {
        "expected_doc_class": row["expected_doc_class"],
        "expected_stage": row["expected_stage"],
        "expected_subclass": row.get("expected_subclass") or "",
        "expected_fields": expected_fields,
        "manifest_id": row["id"],
    }
    document = {
        "filename": local.name,
        "matter_id": f"LIVE-{_safe(row['id'])}",
        "source": "live-manifest",
        "manifest_id": row["id"],
        "content_sha256": row["content_sha256"],
        "hub_revision": row.get("hub_revision") or revision.get("tag") or revision.get("sha"),
    }
    recorder = RunEventRecorder(
        document=document,
        ground_truth=ground_truth,
        runs_dir=base / "diagnostics" / "runs",
    )
    dataset = {
        "name": DATASET_ID,
        "revision": document["hub_revision"],
        "taxonomy_version": "mailroom-dataset",
    }

    if mode == "mock":
        from langchain_agents.base_agent import BaseAgent as LangChainBaseAgent
        from langchain_agents.mock import FakeLangChainLLM

        classification, extraction = _mock_expect(row)

        def get_llm(agent_name: str):
            return _mock_client(agent_name, classification, extraction), "smoke-mock"

        def langchain_llm(_self):
            return FakeLangChainLLM(
                classification=classification,
                extraction={**extraction, "confidence": 0.99},
            )

        with patch("llm.client.get_llm", side_effect=get_llm), patch(
            "agents.base.get_llm", side_effect=get_llm
        ), patch.object(LangChainBaseAgent, "llm", new=langchain_llm):
            result = run_pipeline(
                local,
                matter_id=document["matter_id"],
                source="live-manifest",
                ground_truth=ground_truth,
                session_id=f"live-smoke-{row['id']}",
                run_id=f"live-{row['id']}",
                dataset=dataset,
                run_events=recorder,
            )
    else:
        import scripts.run_pilot as pilot

        with patch("llm.client.get_llm", side_effect=pilot._real_get_llm), patch(
            "agents.base.get_llm", side_effect=pilot._real_get_llm
        ), patch.object(
            __import__("langchain_agents.base_agent", fromlist=["BaseAgent"]).BaseAgent,
            "llm",
            new=pilot._make_real_langchain_llm(),
        ):
            result = run_pipeline(
                local,
                matter_id=document["matter_id"],
                source="live-manifest",
                ground_truth=ground_truth,
                session_id=f"live-smoke-{row['id']}",
                run_id=f"live-{row['id']}",
                dataset=dataset,
                run_events=recorder,
            )
    journal = recorder.payload or recorder.finalize(result)
    return result, journal


def _checks(row: dict[str, Any], result: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    sequence = journal.get("node_sequence") or []
    stage = result.get("stage")
    checks = {
        "class_ok": result.get("doc_type") == row["expected_doc_class"],
        "stage_ok": stage == row["expected_stage"],
        "intake_first": bool(sequence) and sequence[0] == "intake",
        "classify_present": "classify" in sequence,
        "extract_present": "extract" in sequence,
        "terminal_node_ok": (sequence[-1] == "archive") if stage == "archived" else True,
        "journal_written": bool(journal.get("events")),
        "outcome_matches_journal": journal.get("outcome", {}).get("stage") == stage,
    }
    return checks


def _run(args: argparse.Namespace) -> int:
    base = Path(args.base).expanduser().resolve() if args.base else Path(
        tempfile.mkdtemp(prefix="mailroom-live-smoke-")
    )
    base.mkdir(parents=True, exist_ok=True)
    os.environ["MAILROOM_BASE_DIR"] = str(base)
    os.environ["MAILROOM_RELATIONS"] = "0"
    os.environ["MAILROOM_RELATIONS_EMBEDDINGS"] = "0"
    os.environ["MAILROOM_GMAIL_ENABLED"] = "0"
    os.environ["OBSERVABILITY_PROVIDER"] = "none" if not args.real else os.environ.get(
        "OBSERVABILITY_PROVIDER", "auto"
    )

    stamp = _now()
    out = Path(args.out).expanduser() if args.out else Path("data") / "live_smoke" / stamp
    out.mkdir(parents=True, exist_ok=True)

    rows, revision, source = _load(args, select=not args.replay)
    if args.replay:
        return _replay(Path(args.replay), rows, revision, base, out, args)

    texts = fetch_texts(rows, revision)
    runs = []
    for row in rows:
        text = texts[row["filename"]]
        result, journal = _run_one(row, text, base=base, mode="real" if args.real else "mock", revision=revision)
        checks = _checks(row, result, journal)
        runs.append(
            {
                "manifest_id": row["id"],
                "filename": row["filename"],
                "expected": row["expected_doc_class"],
                "expected_subclass": row.get("expected_subclass"),
                "expected_stage": row["expected_stage"],
                "predicted": result.get("doc_type"),
                "stage": result.get("stage"),
                "execution_id": journal.get("execution_id"),
                "journal": str(base / "diagnostics" / "runs"),
                "node_sequence": journal.get("node_sequence") or [],
                "outcome": journal.get("outcome"),
                "checks": checks,
                "ok": all(checks.values()),
            }
        )

    metrics = {
        "n": len(runs),
        "class_ok": sum(1 for run in runs if run["checks"]["class_ok"]),
        "stage_ok": sum(1 for run in runs if run["checks"]["stage_ok"]),
        "all_checks_ok": all(run["ok"] for run in runs),
        "classes": sorted({run["expected"] for run in runs}),
    }
    experiment = {
        "schema_version": "1.0.0",
        "experiment_id": stamp,
        "generated_at": stamp,
        "mode": "real" if args.real else "mock",
        "corpus": {
            "name": DATASET_ID,
            "revision": revision,
            "source": source,
        },
        "base_dir": str(base),
        "runs": runs,
        "metrics": metrics,
    }
    experiment_path = out / "experiment.json"
    experiment_path.write_text(json.dumps(experiment, indent=2) + "\n", encoding="utf-8")
    experiments_dir = base / "diagnostics" / "experiments"
    experiments_dir.mkdir(parents=True, exist_ok=True)
    (experiments_dir / f"{stamp}.json").write_text(json.dumps(experiment, indent=2) + "\n", encoding="utf-8")
    summary = {
        "experiment": str(experiment_path),
        "base_dir": str(base),
        "runs": len(runs),
        "metrics": metrics,
        "revision": revision,
    }
    print(json.dumps(summary, indent=2))
    return 0 if metrics["all_checks_ok"] else 1


def _replay(
    journal_path: Path,
    rows: list[dict[str, Any]],
    revision: dict[str, Any],
    base: Path,
    out: Path,
    args: argparse.Namespace,
) -> int:
    prior = json.loads(journal_path.read_text(encoding="utf-8"))
    document = prior.get("document") or {}
    manifest_id = document.get("manifest_id") or ""
    row = next((row for row in rows if row["id"] == manifest_id), None)
    if row is None:
        row = next((row for row in rows if row["filename"] == document.get("filename")), None)
    if row is None:
        raise SystemExit(f"replay document {manifest_id or document.get('filename')!r} not in manifest")
    texts = fetch_texts([row], revision)
    result, journal = _run_one(row, texts[row["filename"]], base=base, mode="real" if args.real else "mock", revision=revision)
    old_sequence = prior.get("node_sequence") or []
    new_sequence = journal.get("node_sequence") or []
    diff = {
        "journal": str(journal_path),
        "document": manifest_id or row["id"],
        "same_sequence": old_sequence == new_sequence,
        "same_stage": (prior.get("outcome") or {}).get("stage") == result.get("stage"),
        "same_class": (prior.get("outcome") or {}).get("doc_type") == result.get("doc_type"),
        "old_sequence": old_sequence,
        "new_sequence": new_sequence,
        "old_outcome": prior.get("outcome"),
        "new_outcome": journal.get("outcome"),
    }
    path = out / "replay.json"
    path.write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**diff, "replay_report": str(path)}, indent=2))
    return 0 if diff["same_sequence"] and diff["same_stage"] and diff["same_class"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=FIXTURE)
    parser.add_argument("--live", action="store_true", help="pull the newest tagged corpus")
    parser.add_argument("--revision", help="tag/branch/commit for --live")
    parser.add_argument("--per-class", type=int, default=1)
    parser.add_argument("--classes", default=",".join(CLASSES))
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--base", type=Path, help="mailroom base directory (default temp)")
    parser.add_argument("--out", type=Path, help="report directory")
    parser.add_argument("--mock", action="store_true", help="deterministic mock LLM (default)")
    parser.add_argument("--real", action="store_true", help="use the real LLM provider")
    parser.add_argument("--replay", type=Path, help="replay a recorded run journal and diff it")
    args = parser.parse_args(argv)
    args.classes = [part.strip() for part in args.classes.split(",") if part.strip()]
    if args.mock and args.real:
        parser.error("--mock and --real are mutually exclusive")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
