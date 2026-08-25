#!/usr/bin/env python3
"""Hugging Face docclass-merged pilot — the runner The-Mailroom orchestrates.

``scripts/run_production_pilot.py`` in The-Mailroom looks for this file and
invokes ``--check`` / ``--real --per-class N``. Traces land in Langfuse under
session ``pilot-hf-<UTC stamp>`` with tags ``mailroom``, ``pilot``,
``source-docclass-merged`` (and ``docclass-prompts`` when that arm is on)
so the visualizer FLOOR / TUI / Observatory can score them.

  --check   network-free contract (intake + scorer mapping + report schema)
  --mock    pipeline machinery on tiny in-repo fixtures (no Hub, fake LLM)
  --real    live Qwen via OpenRouter on a stratified HF subset
  --docclass  opt-in KANBAN-090 docclass prompt variants for every agent

Usage:
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --check
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --mock --per-class 1
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 1
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 5 --docclass --max-scan 4000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

DATASET_ID = "Lucius-Morningstar/docclass-merged"
VIEWER_BASE = "https://datasets-server.huggingface.co"
HF_CLASSES = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)
# Live mailroom taxonomy files MAUD/merger rows as contract. The visualizer
# still scores exact vs aligned (merger_agreement ≡ contract).
ALIGN = {"merger_agreement": "contract"}

_MOCK_DOCS = {
    "contract": (
        "hf_contract.txt",
        "SERVICES AGREEMENT\n\nThis Services Agreement is entered into as of "
        "January 1, 2024, by and between Acme Corp (\"Provider\") and Beta LLC "
        "(\"Customer\"). Provider shall perform the services described in Exhibit A. "
        "This Agreement is governed by the laws of Delaware.",
    ),
    "merger_agreement": (
        "hf_merger_agreement.txt",
        "AGREEMENT AND PLAN OF MERGER\n\nThis Agreement and Plan of Merger is "
        "entered into by Parent Inc., Merger Sub Inc., and Target Corp. At the "
        "Effective Time, Merger Sub shall merge with and into Target, and Target "
        "shall be the surviving corporation. The merger consideration is all cash.",
    ),
    "corporate_record": (
        "hf_corporate_record.txt",
        "BYLAWS OF REVENUE.COM CORPORATION\n\nA Nevada Corporation\n\nARTICLE I "
        "STOCKHOLDERS\nSection 1. Annual meetings of the stockholders of the "
        "Corporation shall be held on a date set by the Board of Directors.",
    ),
    "correspondence": (
        "hf_correspondence.txt",
        "From: Jane Counsel <jane@firm.com>\nTo: Opposing Counsel\nDate: March 3, 2024\n"
        "Subject: Demand for payment\n\nDear Counsel,\nThis letter demands payment of "
        "the outstanding invoice under the parties' services agreement within 14 days.",
    ),
    "insurance_claim": (
        "hf_insurance_claim.txt",
        "CMS MEDICARE OUTPATIENT CLAIM\nInsurer: CMS Medicare\nInsured: LOPEZ, PATRICIA\n"
        "Policy: 9C4BA446BC00112E\nClaim type: health\nDate of service: 2008-06-10\n"
        "Diagnosis: congestive heart failure. No named adjuster is listed.",
    ),
}


def pipeline_class(hf_class: str) -> str:
    return ALIGN.get(hf_class, hf_class)


def _as_meta(row: dict) -> dict:
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def parse_hf_row(row: dict, labels: dict[str, dict] | None = None) -> dict | None:
    """Normalize a Dataset Viewer / datasets row into a sample dict.

    Docclass / subclass ground truth comes from the Hub ``ground_truth``
    config fields ``expected`` and ``expected_subclass`` (joined on
    filename). Default-config ``metadata.expected_doc_type`` is a fallback
    only when it is one of the five HF classes — CUAD folder names
    (``License_Agreements``, ``inbox``, …) are never treated as classes.
    """
    if not isinstance(row, dict):
        return None
    text = row.get("doc_text") or row.get("text") or ""
    if not str(text).strip():
        return None
    filename = str(row.get("filename") or row.get("id") or "doc.txt")
    meta = _as_meta(row)
    gt = (labels or {}).get(filename) or {}
    hf_class = (
        gt.get("expected")
        or row.get("expected")
        or row.get("expected_doc_type")
        or row.get("expected_doc_class")
        or row.get("label")
        or row.get("doc_class")
        or meta.get("expected_doc_type")
        or meta.get("expected_doc_class")
    )
    if not hf_class:
        return None
    hf_class = str(hf_class).strip()
    if hf_class not in HF_CLASSES:
        return None
    subclass = (
        gt.get("expected_subclass")
        or row.get("expected_subclass")
        or meta.get("expected_subclass")
        or ""
    )
    return {
        "filename": filename,
        "text": str(text),
        "expected_hf_class": hf_class,
        "expected_subclass": str(subclass).strip() if subclass else "",
        "chars": len(str(text)),
    }


def select_stratified(
    rows: list[dict],
    *,
    per_class: int,
    max_chars: int,
    target_chars: int,
    classes: tuple[str, ...] = HF_CLASSES,
) -> list[dict]:
    """Pick ``per_class`` docs per HF label closest to ``target_chars``."""
    buckets: dict[str, list[dict]] = {c: [] for c in classes}
    for row in rows:
        cls = row.get("expected_hf_class")
        if cls not in buckets:
            continue
        n = int(row.get("chars") or 0)
        if n < 200:
            continue
        # Oversized docs stay in the pool (MAUD mergers are 100k–1M chars).
        # ``max_chars`` truncates the text at run time, not the sample set.
        buckets[cls].append(row)
    selected: list[dict] = []
    for cls in classes:
        cands = list(buckets[cls])
        cands.sort(key=lambda r: abs(int(r["chars"]) - target_chars))
        selected.extend(cands[:per_class])
    return selected


def _safe_filename(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name or "doc.txt"
    if not Path(base).suffix:
        base += ".txt"
    base = re.sub(r"[^\w.\-]+", "_", base)
    return base[:180] or "doc.txt"


def _mock_samples(per_class: int) -> list[dict]:
    out = []
    for hf_class, (filename, text) in _MOCK_DOCS.items():
        for i in range(per_class):
            name = filename if i == 0 else f"{Path(filename).stem}_{i}{Path(filename).suffix}"
            out.append({
                "filename": name,
                "text": text,
                "expected_hf_class": hf_class,
                "expected_subclass": "fixture",
                "chars": len(text),
            })
    return out


def _viewer_rows(split: str, offset: int, length: int, *, config: str = "default") -> dict:
    import httpx

    params = {
        "dataset": DATASET_ID,
        "config": config,
        "split": split,
        "offset": offset,
        "length": min(int(length), 100),
    }
    headers = {}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(f"{VIEWER_BASE}/rows", params=params, headers=headers, timeout=45)
    resp.raise_for_status()
    return resp.json()


def _paginate_viewer(*, split: str, max_scan: int, config: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while offset < max_scan:
        payload = _viewer_rows(split, offset, min(100, max_scan - offset), config=config)
        batch = payload.get("rows") or []
        if not batch:
            break
        for item in batch:
            row = item.get("row") if isinstance(item, dict) else item
            if isinstance(row, dict):
                rows.append(row)
        offset += len(batch)
        total = payload.get("num_rows_total")
        if total is not None and offset >= int(total):
            break
        if len(batch) < 1:
            break
    return rows


def load_ground_truth_labels(*, split: str, max_scan: int) -> dict[str, dict]:
    """Map filename → {expected, expected_subclass} from config=ground_truth.

    These are the Hub's canonical docclass / subclass labels. ``expected`` is
    one of the five HF classes; ``expected_subclass`` is the second-level
    label (CUAD family, record type, claim subtype, merger consideration).
    """
    labels: dict[str, dict] = {}
    try:
        from datasets import load_dataset  # type: ignore

        ds = load_dataset(DATASET_ID, "ground_truth", split=split)
        raw_rows = [dict(row) for i, row in enumerate(ds) if i < max_scan]
    except Exception:
        raw_rows = _paginate_viewer(split=split, max_scan=max_scan, config="ground_truth")
    for row in raw_rows:
        filename = str(row.get("filename") or "").strip()
        expected = str(row.get("expected") or "").strip()
        if not filename or expected not in HF_CLASSES:
            continue
        labels[filename] = {
            "expected": expected,
            "expected_subclass": str(row.get("expected_subclass") or "").strip(),
        }
    return labels


def load_hf_rows(*, split: str, max_scan: int) -> list[dict]:
    """Load default-config text rows joined to ground_truth labels on filename."""
    labels = load_ground_truth_labels(split=split, max_scan=max_scan)
    parsed: list[dict] = []
    try:
        from datasets import load_dataset  # type: ignore

        ds = load_dataset(DATASET_ID, split=split)
        default_rows = [dict(row) for i, row in enumerate(ds) if i < max_scan]
    except Exception:
        default_rows = _paginate_viewer(split=split, max_scan=max_scan, config="default")
    for row in default_rows:
        item = parse_hf_row(row, labels)
        if item:
            parsed.append(item)
    return parsed


def _report_root() -> Path:
    override = os.environ.get("MAILROOM_HF_PILOT_DIR")
    if override:
        return Path(override)
    return REPO_ROOT / "data" / "hf_pilot"


def check_contract() -> int:
    from agents.intake import deterministic_normalize, looks_messy

    cleaned, stats = deterministic_normalize("A\u00a0B\n\n\n\nagree-\nment")
    assert "A B" in cleaned
    assert "agreement" in cleaned
    assert stats["changed"] is True
    assert looks_messy("x\n" * 30) is True
    assert pipeline_class("merger_agreement") == "contract"
    assert pipeline_class("insurance_claim") == "insurance_claim"
    rows = [
        {"expected_hf_class": c, "chars": 6000 if c != "contract" else 5900, "filename": f"{c}.txt"}
        for c in HF_CLASSES
    ]
    picked = select_stratified(rows, per_class=1, max_chars=25000, target_chars=6000)
    assert {r["expected_hf_class"] for r in picked} == set(HF_CLASSES)
    report_keys = {
        "session_id", "run_id", "dataset", "split", "mode", "samples",
    }
    sample_keys = {
        "trace_id", "filename", "local_filename", "expected",
        "expected_doc_class", "expected_subclass", "predicted", "stage",
    }
    print("check ok", json.dumps({
        "intake": True,
        "align": ALIGN,
        "report_keys": sorted(report_keys),
        "sample_keys": sorted(sample_keys),
        "dataset": DATASET_ID,
        "n_classes": len(HF_CLASSES),
    }))
    return 0


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars and len(text) > max_chars:
        return text[:max_chars]
    return text


def _run_one(sample: dict, *, mock_mode: bool, session_id: str, run_id: str, matter_id: str, max_chars: int = 25000) -> dict:
    import shutil
    from unittest.mock import patch

    from graph.build_graph import run_pipeline
    from pipeline.bins import inbox_dir
    import scripts.run_pilot as rp

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    local_name = _safe_filename(sample["filename"])
    queued = inbox / local_name
    queued.write_text(_truncate_text(sample["text"], max_chars), encoding="utf-8")

    hf_class = sample["expected_hf_class"]
    expect_type = pipeline_class(hf_class)
    expect = {"doc_type": expect_type, "conf": 0.96}
    ground_truth = {
        "expected": hf_class,
        "expected_doc_class": expect_type,
        "expected_hf_class": hf_class,
    }
    if sample.get("expected_subclass"):
        ground_truth["expected_subclass"] = sample["expected_subclass"]

    rp._LLM_METRICS["calls"] = 0
    rp._LLM_METRICS["seconds"] = 0.0
    rp._LLM_METRICS["usage"] = []
    rp._LLM_METRICS["cost_usd"] = 0.0

    def _mock_get_llm(agent_name):
        return rp._fake_client(expect), "mock-model"

    started = time.perf_counter()
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent

    if mock_mode:
        with patch("llm.client.get_llm", side_effect=_mock_get_llm), \
             patch("agents.base.get_llm", side_effect=_mock_get_llm), \
             patch.object(_LangChainBaseAgent, "llm", new=rp._make_mock_langchain_llm(expect)):
            result = run_pipeline(
                queued, matter_id, source="docclass-merged",
                ground_truth=ground_truth, session_id=session_id, run_id=run_id,
            )
    else:
        with patch("llm.client.get_llm", side_effect=rp._real_get_llm), \
             patch("agents.base.get_llm", side_effect=rp._real_get_llm), \
             patch.object(_LangChainBaseAgent, "llm", new=rp._make_real_langchain_llm()):
            result = run_pipeline(
                queued, matter_id, source="docclass-merged",
                ground_truth=ground_truth, session_id=session_id, run_id=run_id,
            )
    wall = time.perf_counter() - started
    predicted = result.get("doc_type")
    return {
        "trace_id": result.get("trace_id"),
        "filename": sample["filename"],
        "local_filename": local_name,
        "expected": hf_class,
        "expected_doc_class": expect_type,
        "expected_subclass": sample.get("expected_subclass") or "",
        "predicted": predicted,
        "stage": result.get("stage"),
        "classification_confidence": result.get("classification_confidence"),
        "extraction_confidence": result.get("extraction_confidence"),
        "exact_ok": predicted == hf_class,
        "aligned_ok": pipeline_class(hf_class) == predicted or hf_class == predicted,
        "wall_time_s": round(wall, 3),
        "llm_calls": rp._LLM_METRICS["calls"],
        "llm_cost_usd": round(rp._LLM_METRICS["cost_usd"], 6),
        "error": result.get("error_message"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--mock", action="store_true")
    parser.add_argument("--per-class", type=int, default=1)
    parser.add_argument("--split", default="train")
    parser.add_argument("--max-chars", type=int, default=25000)
    parser.add_argument("--target-chars", type=int, default=6000)
    parser.add_argument("--max-scan", type=int, default=1500)
    parser.add_argument(
        "--docclass",
        action="store_true",
        help="Use KANBAN-090 docclass prompt variants (MAILROOM_DOCCLASS_PROMPTS=1).",
    )
    args = parser.parse_args()

    if args.docclass:
        os.environ["MAILROOM_DOCCLASS_PROMPTS"] = "1"

    if args.check:
        return check_contract()

    from pipeline.env import default_environment, load_env
    from pipeline.logging import setup_logging

    load_env()
    default_environment("pilot")
    setup_logging()
    os.environ.setdefault("MAILROOM_VISION_ENABLED", "0")

    mock_mode = bool(args.mock)
    if not mock_mode:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key or key == "mock-key":
            parser.error("OPENROUTER_API_KEY is not set to a real key — refusing --real")

    import scripts.run_pilot as rp

    abort = float(os.environ.get("MAILROOM_PILOT_COST_ABORT", "2.00"))
    rp._COST_ABORT_USD = abort
    rp._COST_WARN_USD = abort * 0.75
    rp._RUN_COST_USD["value"] = 0.0
    rp._RUN_COST_USD["warned"] = False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"pilot-hf-{stamp}"
    run_id = stamp
    matter_id = f"hf-docclass-merged-{stamp}"

    if mock_mode:
        samples = _mock_samples(max(1, args.per_class))
    else:
        raw = load_hf_rows(split=args.split, max_scan=args.max_scan)
        samples = select_stratified(
            raw,
            per_class=max(1, args.per_class),
            max_chars=args.max_chars,
            target_chars=args.target_chars,
        )
        got = {c: 0 for c in HF_CLASSES}
        for s in samples:
            got[s["expected_hf_class"]] = got.get(s["expected_hf_class"], 0) + 1
        missing = [c for c in HF_CLASSES if got.get(c, 0) < max(1, args.per_class)]
        if missing:
            raise SystemExit(
                f"HF subset incomplete for {DATASET_ID} split={args.split} "
                f"per_class={args.per_class}: got {len(samples)} samples "
                f"(by class {got}); short classes {missing}. "
                "Labels must come from config=ground_truth (expected / "
                "expected_subclass), joined to default-config doc_text."
            )

    rows = []
    errors = 0
    for sample in samples:
        try:
            rows.append(_run_one(
                sample, mock_mode=mock_mode, session_id=session_id,
                run_id=run_id, matter_id=matter_id, max_chars=args.max_chars,
            ))
        except Exception as exc:
            errors += 1
            rows.append({
                "filename": sample.get("filename"),
                "local_filename": _safe_filename(sample.get("filename") or "doc.txt"),
                "expected": sample.get("expected_hf_class"),
                "expected_doc_class": pipeline_class(sample.get("expected_hf_class") or ""),
                "expected_subclass": sample.get("expected_subclass") or "",
                "predicted": None,
                "stage": "error",
                "error": str(exc)[:400],
                "exact_ok": False,
                "aligned_ok": False,
            })

    from pipeline.docclass_mode import docclass_prompts_enabled

    report = {
        "session_id": session_id,
        "run_id": run_id,
        "matter_id": matter_id,
        "dataset": DATASET_ID,
        "split": args.split,
        "mode": "mock" if mock_mode else "real",
        "docclass_prompts": docclass_prompts_enabled(),
        "samples": rows,
        "n": len(rows),
        "errors": errors,
    }
    out_dir = _report_root() / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "session_id": session_id,
        "run_id": run_id,
        "report": str(path),
        "n": len(rows),
        "errors": errors,
        "docclass_prompts": report["docclass_prompts"],
        "stages": {r.get("stage"): 1 for r in rows},
    }))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
