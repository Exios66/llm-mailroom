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
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 10 --docclass --max-scan 4000
    PYTHONPATH=src python src/scripts/run_quality_judges.py --real --hf-latest 5
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

from langchain_agents.cuad_maud import (  # noqa: E402
    flatten_cuad_clause_labels,
    flatten_maud_clause_labels,
    infer_merger_consideration,
    normalize_consideration,
)

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
    cuad_raw = (
        gt.get("cuad_clause_labels")
        if gt.get("cuad_clause_labels") not in (None, "")
        else row.get("cuad_clause_labels")
    )
    maud_raw = (
        gt.get("maud_clause_labels")
        if gt.get("maud_clause_labels") not in (None, "")
        else row.get("maud_clause_labels")
    )
    return {
        "filename": filename,
        "text": str(text),
        "expected_hf_class": hf_class,
        "expected_subclass": str(subclass).strip() if subclass else "",
        "cuad_clauses": flatten_cuad_clause_labels(cuad_raw),
        "maud_clauses": flatten_maud_clause_labels(maud_raw),
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


def _inbox_filename(name: str) -> str:
    """Write Hub extracted text as ``.txt``.

    Source filenames are often ``.PDF`` / ``.htm``. Ingest keys off the
    suffix, so keeping ``.PDF`` sends plaintext through pypdf and yields
    a truncated transcription.
    """
    stem = Path(_safe_filename(name)).stem or "doc"
    return stem[:170] + ".txt"


def _unique_name(desired: str, used: set[str]) -> str:
    """Keep inbox / matter ids unique within a run (scale hardening)."""
    if desired not in used:
        used.add(desired)
        return desired
    stem, suffix = Path(desired).stem, Path(desired).suffix
    n = 2
    while True:
        cand = f"{stem}__{n}{suffix}"
        if cand not in used:
            used.add(cand)
            return cand
        n += 1


def _alnum(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _loose_label_match(predicted, expected) -> bool:
    a, b = _alnum(predicted), _alnum(expected)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a) or b in a or a in b


def subclass_ok(expected_class: str, expected_subclass: str, *, predicted_subtype: str = "", extracted: dict | None = None) -> bool | None:
    """Score Hub ``expected_subclass`` against sorter subtype / extraction.

    Returns None when there is no subclass ground truth to score.
    """
    want = str(expected_subclass or "").strip()
    if not want:
        return None
    extracted = extracted or {}
    hf_class = str(expected_class or "")
    if hf_class == "contract":
        from langchain_agents.sorter_agent import equivalent_subtypes, normalize_subtype

        got = normalize_subtype(predicted_subtype or extracted.get("cuad_family") or extracted.get("contract_subtype"))
        need = normalize_subtype(want)
        return equivalent_subtypes(got, need)
    if hf_class == "merger_agreement":
        got = (
            normalize_consideration(extracted.get("merger_consideration"))
            or infer_merger_consideration(extracted)
            or normalize_consideration(predicted_subtype)
        )
        need = normalize_consideration(want)
        if not got or not need:
            return False
        return got == need
    if hf_class == "corporate_record":
        return _loose_label_match(extracted.get("record_type") or predicted_subtype, want)
    if hf_class == "insurance_claim":
        return _loose_label_match(extracted.get("claim_type") or predicted_subtype, want)
    if hf_class == "correspondence":
        return _loose_label_match(extracted.get("communication_type") or predicted_subtype, want)
    return _loose_label_match(predicted_subtype, want)


def _public_extracted(data) -> dict:
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if not str(key).startswith("_") and key != "reasoning"
    }


def _usage_tokens(usage: list) -> int:
    total = 0
    for item in usage or []:
        if not isinstance(item, dict):
            continue
        total += int(item.get("prompt_tokens") or 0) + int(item.get("completion_tokens") or 0)
    return total


def summarize_rows(rows: list[dict]) -> dict:
    """Exact / aligned / subclass accuracy plus cost and stage mix."""
    from collections import Counter

    n = len(rows)
    exact_n = sum(1 for r in rows if r.get("exact_ok"))
    aligned_n = sum(1 for r in rows if r.get("aligned_ok"))
    subclass_scored = [r for r in rows if r.get("subclass_ok") is not None]
    subclass_n = sum(1 for r in subclass_scored if r.get("subclass_ok"))
    costs = [float(r["llm_cost_usd"]) for r in rows if isinstance(r.get("llm_cost_usd"), (int, float))]
    tokens = [int(r["llm_tokens"]) for r in rows if isinstance(r.get("llm_tokens"), (int, float))]
    calls = [int(r["llm_calls"]) for r in rows if isinstance(r.get("llm_calls"), (int, float))]
    walls = [float(r["wall_time_s"]) for r in rows if isinstance(r.get("wall_time_s"), (int, float))]
    per_class: dict[str, dict] = {}
    for row in rows:
        cls = row.get("expected") or "unknown"
        bucket = per_class.setdefault(cls, {
            "n": 0, "exact": 0, "aligned": 0, "subclass": 0, "subclass_n": 0,
            "cost_usd": 0.0, "tokens": 0, "stages": Counter(),
        })
        bucket["n"] += 1
        bucket["exact"] += int(bool(row.get("exact_ok")))
        bucket["aligned"] += int(bool(row.get("aligned_ok")))
        if row.get("subclass_ok") is not None:
            bucket["subclass_n"] += 1
            bucket["subclass"] += int(bool(row.get("subclass_ok")))
        bucket["cost_usd"] += float(row.get("llm_cost_usd") or 0)
        bucket["tokens"] += int(row.get("llm_tokens") or 0)
        bucket["stages"][row.get("stage") or "unknown"] += 1
    return {
        "n": n,
        "exact_n": exact_n,
        "aligned_n": aligned_n,
        "exact_accuracy": round(exact_n / n, 3) if n else 0.0,
        "aligned_accuracy": round(aligned_n / n, 3) if n else 0.0,
        "subclass_n": len(subclass_scored),
        "subclass_correct": subclass_n,
        "subclass_accuracy": round(subclass_n / len(subclass_scored), 3) if subclass_scored else None,
        "total_cost_usd": round(sum(costs), 6),
        "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else 0.0,
        "total_tokens": int(sum(tokens)),
        "total_llm_calls": int(sum(calls)),
        "avg_wall_time_s": round(sum(walls) / len(walls), 3) if walls else 0.0,
        "stages": dict(Counter(r.get("stage") or "unknown" for r in rows)),
        "per_class": {
            cls: {
                "n": v["n"],
                "exact": v["exact"],
                "aligned": v["aligned"],
                "exact_accuracy": round(v["exact"] / v["n"], 3) if v["n"] else 0.0,
                "aligned_accuracy": round(v["aligned"] / v["n"], 3) if v["n"] else 0.0,
                "subclass_accuracy": (
                    round(v["subclass"] / v["subclass_n"], 3) if v["subclass_n"] else None
                ),
                "cost_usd": round(v["cost_usd"], 6),
                "tokens": v["tokens"],
                "stages": dict(v["stages"]),
            }
            for cls, v in sorted(per_class.items())
        },
    }


def find_sample_text(sample: dict, report_dir: Path | None = None) -> str:
    """Recover source text for an HF pilot row (archive / review / failed)."""
    for key in ("archive_path", "file_path", "source_path"):
        raw = sample.get(key)
        if raw:
            path = Path(str(raw))
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
    name = sample.get("local_filename") or sample.get("filename") or ""
    name = Path(str(name)).name
    if not name:
        return str(sample.get("doc_text") or sample.get("text") or "")
    roots: list[Path] = []
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data"))
    roots.extend([base / "archive", base / "review", base / "failed"])
    if report_dir is not None:
        roots.append(Path(report_dir))
    for root in roots:
        if not root.exists():
            continue
        matches = list(root.rglob(name))
        if matches:
            try:
                return matches[0].read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return str(sample.get("doc_text") or sample.get("text") or "")


def latest_hf_reports(n: int = 5, root: Path | None = None) -> list[Path]:
    base = root or _report_root()
    if not base.exists():
        return []
    dirs = sorted(
        (p for p in base.iterdir() if p.is_dir() and (p / "report.json").is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    return [d / "report.json" for d in dirs[: max(0, n)]]


def hf_samples_from_report(report: dict, report_path: Path | None = None) -> list[dict]:
    """Turn an HF ``report.json`` into judge-ready sample dicts."""
    report_dir = report_path.parent if report_path else None
    samples = []
    for row in report.get("samples") or []:
        filename = row.get("local_filename") or row.get("filename") or "doc.txt"
        catalog = _catalog_by_trace(row.get("trace_id") or "")
        extracted = row.get("extracted_data") or catalog.get("extracted_data") or {}
        text = find_sample_text(row, report_dir)
        if not text and catalog.get("original_filename"):
            text = find_sample_text({**row, "filename": catalog["original_filename"]}, report_dir)
        samples.append({
            "id": filename,
            "filename": filename,
            "doc_type": row.get("predicted") or row.get("expected_doc_class") or "",
            "extracted_data": _public_extracted(extracted),
            "trace_id": row.get("trace_id"),
            "doc_text": text,
            "expected": row.get("expected"),
            "expected_subclass": row.get("expected_subclass") or "",
            "subdir": "",
        })
    return samples


def _catalog_by_trace(trace_id: str) -> dict:
    """Best-effort SQLite lookup so older HF reports without extracted_data still judge."""
    if not trace_id:
        return {}
    db = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "mailroom.db"
    if not db.exists():
        return {}
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT extracted_data, contract_subtype, original_filename "
                "FROM documents WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return {}
    if not row:
        return {}
    data = row[0]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    return {
        "extracted_data": data or {},
        "contract_subtype": row[1] or "",
        "original_filename": row[2] or "",
    }


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
    last: Exception | None = None
    for attempt in range(5):
        try:
            resp = httpx.get(f"{VIEWER_BASE}/rows", params=params, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"HF viewer failed after 5 tries ({config}/{split} offset={offset}): {last}")


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
            "cuad_clause_labels": row.get("cuad_clause_labels"),
            "maud_clause_labels": row.get("maud_clause_labels"),
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


def _run_one(sample: dict, *, mock_mode: bool, session_id: str, run_id: str, matter_id: str, max_chars: int = 25000, local_name: str | None = None) -> dict:
    from unittest.mock import patch

    from graph.build_graph import run_pipeline
    from pipeline.bins import inbox_dir
    import scripts.run_pilot as rp

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    local_name = local_name or _inbox_filename(sample["filename"])
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
    expected_fields: dict = {}
    if sample.get("cuad_clauses"):
        expected_fields["cuad_clauses"] = list(sample["cuad_clauses"])
    if sample.get("maud_clauses"):
        expected_fields["maud_clauses"] = list(sample["maud_clauses"])
    if hf_class == "contract" and sample.get("expected_subclass"):
        from langchain_agents.sorter_agent import normalize_subtype

        expected_fields["cuad_family"] = normalize_subtype(sample["expected_subclass"])
    if hf_class == "merger_agreement" and sample.get("expected_subclass"):
        token = normalize_consideration(sample["expected_subclass"])
        if token:
            expected_fields["merger_consideration"] = token
    if expected_fields:
        ground_truth["expected_fields"] = expected_fields

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
    extracted = _public_extracted(result.get("extracted_data"))
    subtype = result.get("contract_subtype") or ""
    file_path = result.get("file_path") or ""
    subclass = subclass_ok(
        hf_class, sample.get("expected_subclass") or "",
        predicted_subtype=subtype, extracted=extracted,
    )
    return {
        "trace_id": result.get("trace_id"),
        "doc_id": result.get("doc_id"),
        "matter_id": matter_id,
        "filename": sample["filename"],
        "local_filename": local_name,
        "file_path": file_path,
        "expected": hf_class,
        "expected_doc_class": expect_type,
        "expected_subclass": sample.get("expected_subclass") or "",
        "cuad_clauses": sample.get("cuad_clauses") or [],
        "maud_clauses": sample.get("maud_clauses") or [],
        "predicted": predicted,
        "predicted_subtype": subtype,
        "stage": result.get("stage"),
        "classification_confidence": result.get("classification_confidence"),
        "extraction_confidence": result.get("extraction_confidence"),
        "extracted_data": extracted,
        "exact_ok": predicted == hf_class,
        "aligned_ok": pipeline_class(hf_class) == predicted or hf_class == predicted,
        "subclass_ok": subclass,
        "wall_time_s": round(wall, 3),
        "llm_calls": rp._LLM_METRICS["calls"],
        "llm_cost_usd": round(rp._LLM_METRICS["cost_usd"], 6),
        "llm_tokens": _usage_tokens(rp._LLM_METRICS["usage"]),
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
    parser.add_argument(
        "--shared-matter",
        action="store_true",
        help="Put every document on one matter_id (exercises Boss same-class "
             "conflicts). Default is a unique matter per document so scaled "
             "evals do not park later same-class docs in REVIEW.",
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
    run_matter = f"hf-docclass-merged-{stamp}"
    unique_matters = not args.shared_matter

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
    used_names: set[str] = set()
    used_matters: set[str] = set()
    out_dir = _report_root() / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    def _flush_report() -> Path:
        report = {
            "session_id": session_id,
            "run_id": run_id,
            "matter_id": run_matter,
            "unique_matters": unique_matters,
            "dataset": DATASET_ID,
            "split": args.split,
            "mode": "mock" if mock_mode else "real",
            "docclass_prompts": docclass_prompts_enabled(),
            "samples": rows,
            "n": len(rows),
            "errors": errors,
            "metrics": summarize_rows(rows),
        }
        path = out_dir / "report.json"
        tmp = out_dir / "report.json.tmp"
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    from pipeline.docclass_mode import docclass_prompts_enabled

    for sample in samples:
        local_name = _unique_name(_inbox_filename(sample.get("filename") or "doc.txt"), used_names)
        if unique_matters:
            matter_id = _unique_name(f"{run_matter}-{Path(local_name).stem}"[:120], used_matters)
        else:
            matter_id = run_matter
        try:
            rows.append(_run_one(
                sample, mock_mode=mock_mode, session_id=session_id,
                run_id=run_id, matter_id=matter_id, max_chars=args.max_chars,
                local_name=local_name,
            ))
        except Exception as exc:
            errors += 1
            rows.append({
                "filename": sample.get("filename"),
                "local_filename": local_name,
                "matter_id": matter_id,
                "expected": sample.get("expected_hf_class"),
                "expected_doc_class": pipeline_class(sample.get("expected_hf_class") or ""),
                "expected_subclass": sample.get("expected_subclass") or "",
                "predicted": None,
                "stage": "error",
                "error": str(exc)[:400],
                "exact_ok": False,
                "aligned_ok": False,
                "subclass_ok": False,
                "llm_cost_usd": 0.0,
                "llm_tokens": 0,
                "llm_calls": 0,
            })
        _flush_report()

    path = _flush_report()
    metrics = summarize_rows(rows)
    print(json.dumps({
        "session_id": session_id,
        "run_id": run_id,
        "report": str(path),
        "n": len(rows),
        "errors": errors,
        "docclass_prompts": docclass_prompts_enabled(),
        "unique_matters": unique_matters,
        "metrics": metrics,
    }))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
