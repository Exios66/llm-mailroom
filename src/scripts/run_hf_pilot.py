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
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --finalize data/hf_pilot/<stamp>
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --resume data/hf_pilot/<stamp> --per-class 10 --docclass --max-scan 4000
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
from langchain_agents.doc_inventories import (  # noqa: E402
    INSURANCE_GT_KEYS,
    coerce_gt_value,
    normalize_claim_type,
    normalize_communication_type,
    normalize_filing_type,
    normalize_record_type,
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
    sample = {
        "filename": filename,
        "text": str(text),
        "expected_hf_class": hf_class,
        "expected_subclass": str(subclass).strip() if subclass else "",
        "cuad_clauses": flatten_cuad_clause_labels(cuad_raw),
        "maud_clauses": flatten_maud_clause_labels(maud_raw),
        "chars": len(str(text)),
    }
    for key in INSURANCE_GT_KEYS:
        raw = gt.get(key)
        if raw in (None, ""):
            raw = row.get(key)
        if raw not in (None, ""):
            sample[key] = coerce_gt_value(raw)
    return sample


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
        got = normalize_record_type(extracted.get("record_type") or predicted_subtype)
        need = normalize_record_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("record_type") or predicted_subtype, want)
    if hf_class == "insurance_claim":
        got = normalize_claim_type(
            extracted.get("claim_type") or predicted_subtype or extracted.get("record_type")
        )
        need = normalize_claim_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("claim_type") or predicted_subtype, want)
    if hf_class == "correspondence":
        got = normalize_communication_type(
            extracted.get("communication_type") or predicted_subtype
        )
        need = normalize_communication_type(want)
        if got and need:
            return got == need
        return _loose_label_match(
            extracted.get("communication_type") or predicted_subtype, want
        )
    if hf_class == "compliance_filing":
        got = normalize_filing_type(extracted.get("filing_type") or predicted_subtype)
        need = normalize_filing_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("filing_type") or predicted_subtype, want)
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
            "cost_usd": 0.0, "tokens": 0, "tokens_known": False, "stages": Counter(),
        })
        bucket["n"] += 1
        bucket["exact"] += int(bool(row.get("exact_ok")))
        bucket["aligned"] += int(bool(row.get("aligned_ok")))
        if row.get("subclass_ok") is not None:
            bucket["subclass_n"] += 1
            bucket["subclass"] += int(bool(row.get("subclass_ok")))
        bucket["cost_usd"] += float(row.get("llm_cost_usd") or 0)
        if isinstance(row.get("llm_tokens"), (int, float)):
            bucket["tokens"] += int(row.get("llm_tokens") or 0)
            bucket["tokens_known"] = True
        bucket["stages"][row.get("stage") or "unknown"] += 1
    scores = [
        float(r["extraction_overall_score"])
        for r in rows
        if isinstance(r.get("extraction_overall_score"), (int, float))
    ]
    out = {
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
        "total_tokens": int(sum(tokens)) if tokens else None,
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
                "tokens": v["tokens"] if v["tokens_known"] else None,
                "stages": dict(v["stages"]),
            }
            for cls, v in sorted(per_class.items())
        },
    }
    if scores:
        out["extraction_n"] = len(scores)
        out["extraction_overall_mean"] = round(sum(scores) / len(scores), 3)
    return out


def expected_fields_for_sample(sample: dict) -> dict:
    """Hub GT clause/family/consideration/subclass payload used as expected_fields."""
    expected_fields: dict = {}
    if sample.get("cuad_clauses"):
        expected_fields["cuad_clauses"] = list(sample["cuad_clauses"])
    if sample.get("maud_clauses"):
        expected_fields["maud_clauses"] = list(sample["maud_clauses"])
    existing = sample.get("expected_fields")
    if isinstance(existing, dict):
        expected_fields.update({k: v for k, v in existing.items() if v not in (None, "")})
    hf_class = sample.get("expected_hf_class") or sample.get("expected") or ""
    subclass = sample.get("expected_subclass") or ""
    if hf_class == "contract" and subclass:
        from langchain_agents.sorter_agent import normalize_subtype

        expected_fields["cuad_family"] = normalize_subtype(subclass)
    if hf_class == "merger_agreement" and subclass:
        token = normalize_consideration(subclass)
        if token:
            expected_fields["merger_consideration"] = token
    if hf_class == "corporate_record" and subclass:
        token = normalize_record_type(subclass)
        expected_fields["record_type"] = token or subclass
    if hf_class == "correspondence" and subclass:
        token = normalize_communication_type(subclass)
        expected_fields["communication_type"] = token or subclass
    if hf_class == "compliance_filing" and subclass:
        token = normalize_filing_type(subclass)
        expected_fields["filing_type"] = token or subclass
    if hf_class == "insurance_claim":
        claim = sample.get("claim_type") or subclass
        token = normalize_claim_type(claim)
        if token or claim:
            expected_fields["claim_type"] = token or claim
        for key in INSURANCE_GT_KEYS:
            if key == "claim_type":
                continue
            val = sample.get(key)
            if val not in (None, ""):
                expected_fields[key] = coerce_gt_value(val)
    return expected_fields


def score_row_extraction(extracted: dict | None, expected_fields: dict | None, doc_class: str) -> dict | None:
    """Deterministic field score; never raises (scaled runs must not die here)."""
    if not expected_fields or not extracted:
        return None
    try:
        from llm_dojo_scoring.field_scoring import score_extraction
        from observability.field_scoring import get_field_types

        scored_class = pipeline_class(doc_class) or doc_class
        result = score_extraction(
            scored_class,
            get_field_types(scored_class),
            extracted,
            expected_fields,
        )
        overall = result.overall_score
        return {
            "overall_score": None if overall is None else round(float(overall), 3),
            "n_fields": len(result.field_scores or {}),
            "needs_judge_review": bool(result.ambiguous_fields),
        }
    except Exception:
        return None


def completed_filenames(rows: list[dict]) -> set[str]:
    """Filenames that already produced a pipeline result (retry errors)."""
    done: set[str] = set()
    for row in rows or []:
        name = row.get("filename")
        if not name:
            continue
        if row.get("stage") in (None, "error"):
            continue
        done.add(str(name))
    return done


def remaining_samples(samples: list[dict], rows: list[dict]) -> list[dict]:
    done = completed_filenames(rows)
    return [s for s in samples if str(s.get("filename") or "") not in done]


def enrich_sample_row(row: dict) -> dict:
    """Backfill subtype / extraction / subclass / field score on older reports."""
    out = dict(row or {})
    catalog = {}
    if not out.get("extracted_data") or not out.get("predicted_subtype"):
        catalog = _catalog_by_trace(str(out.get("trace_id") or ""))
        if catalog.get("extracted_data") and not out.get("extracted_data"):
            out["extracted_data"] = _public_extracted(catalog["extracted_data"])
        if catalog.get("contract_subtype") and not out.get("predicted_subtype"):
            out["predicted_subtype"] = catalog["contract_subtype"]
    extracted = out.get("extracted_data") or {}
    if out.get("subclass_ok") is None and out.get("expected"):
        out["subclass_ok"] = subclass_ok(
            str(out.get("expected") or ""),
            str(out.get("expected_subclass") or ""),
            predicted_subtype=str(out.get("predicted_subtype") or ""),
            extracted=extracted if isinstance(extracted, dict) else {},
        )
    expected_fields = expected_fields_for_sample({
        "expected_hf_class": out.get("expected"),
        "expected_subclass": out.get("expected_subclass"),
        "cuad_clauses": out.get("cuad_clauses") or [],
        "maud_clauses": out.get("maud_clauses") or [],
        **{k: out.get(k) for k in INSURANCE_GT_KEYS if out.get(k) not in (None, "")},
    })
    scored = score_row_extraction(
        extracted if isinstance(extracted, dict) else {},
        expected_fields,
        str(out.get("expected") or out.get("expected_doc_class") or ""),
    )
    if scored:
        out["extraction_overall_score"] = scored["overall_score"]
        out["extraction_n_fields"] = scored["n_fields"]
        out["extraction_needs_judge_review"] = scored["needs_judge_review"]
    return out


def render_metrics_markdown(report: dict) -> str:
    """Human-readable scoring + pricing table for an HF pilot report."""
    rows = [enrich_sample_row(r) for r in (report.get("samples") or [])]
    metrics = summarize_rows(rows)
    session = report.get("session_id") or ""
    lines = [
        f"# HF pilot `{session or report.get('run_id') or 'report'}`",
        "",
        f"- dataset = `{report.get('dataset')}` split `{report.get('split')}`",
        f"- mode = **{report.get('mode')}**  docclass = `{report.get('docclass_prompts')}`  "
        f"unique_matters = `{report.get('unique_matters')}`",
        f"- n = **{metrics.get('n', 0)}**  errors = **{report.get('errors', 0)}**",
        f"- exact accuracy = **{metrics.get('exact_accuracy')}**  "
        f"aligned (merger≡contract) = **{metrics.get('aligned_accuracy')}**  "
        f"subclass = **{metrics.get('subclass_accuracy')}**",
        f"- cost USD = **{metrics.get('total_cost_usd')}**  "
        f"avg $/doc = {metrics.get('avg_cost_usd')}  "
        f"tokens = {metrics.get('total_tokens') if metrics.get('total_tokens') is not None else 'n/a'}  "
        f"LLM calls = {metrics.get('total_llm_calls')}  "
        f"avg wall s = {metrics.get('avg_wall_time_s')}",
    ]
    if metrics.get("extraction_overall_mean") is not None:
        lines.append(
            f"- extraction overall (deterministic) mean = **{metrics['extraction_overall_mean']}** "
            f"over {metrics.get('extraction_n')} grounded docs"
        )
    stages = metrics.get("stages") or {}
    if stages:
        mix = ", ".join(f"{k}={v}" for k, v in sorted(stages.items()))
        lines += ["", f"- stages: {mix}"]
    lines += [
        "",
        "## Per class",
        "",
        "| class | n | exact | aligned | subclass | cost USD | tokens | stages |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for cls, stats in (metrics.get("per_class") or {}).items():
        stage_mix = ", ".join(f"{k}:{v}" for k, v in sorted((stats.get("stages") or {}).items()))
        lines.append(
            f"| {cls} | {stats.get('n')} | {stats.get('exact_accuracy')} | "
            f"{stats.get('aligned_accuracy')} | {stats.get('subclass_accuracy')} | "
            f"{stats.get('cost_usd')} | {stats.get('tokens')} | {stage_mix} |"
        )
    lines += [
        "",
        "## Samples",
        "",
        "| file | expected | predicted | subclass | stage | exact | cost | tokens | extract |",
        "|---|---|---|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        name = str(row.get("filename") or row.get("local_filename") or "")[:56]
        lines.append(
            f"| {name} | {row.get('expected')} | {row.get('predicted')} | "
            f"{row.get('expected_subclass') or ''} | {row.get('stage')} | "
            f"{row.get('exact_ok')} | {row.get('llm_cost_usd')} | "
            f"{row.get('llm_tokens')} | {row.get('extraction_overall_score')} |"
        )
    parked = [r for r in rows if r.get("stage") and r.get("stage") != "archived"]
    if parked:
        lines += ["", "## Non-archive outcomes", ""]
        for row in parked:
            lines.append(
                f"- `{row.get('filename')}` stage={row.get('stage')} "
                f"expected={row.get('expected')} predicted={row.get('predicted')} "
                f"error={row.get('error') or ''}"
            )
    lines.append("")
    return "\n".join(lines)


def write_report_files(path: Path, report: dict) -> Path:
    """Atomic JSON + sidecar markdown so scoring/pricing are always visible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload["metrics"] = summarize_rows(payload.get("samples") or [])
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    md_path = path.with_suffix(".md")
    md_path.write_text(render_metrics_markdown(payload), encoding="utf-8")
    return path


def finalize_report(path: Path) -> dict:
    """Rewrite an existing HF report with metrics, catalog backfill, and markdown."""
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["samples"] = [enrich_sample_row(row) for row in (report.get("samples") or [])]
    report["n"] = len(report["samples"])
    matters = [r.get("matter_id") for r in report["samples"] if r.get("matter_id")]
    if "unique_matters" not in report or report.get("unique_matters") is None:
        if matters:
            report["unique_matters"] = len(set(matters)) == len(matters)
        else:
            report["unique_matters"] = False
    report["metrics"] = summarize_rows(report["samples"])
    write_report_files(path, report)
    return report


def _load_resume(path: Path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    return json.loads(path.read_text(encoding="utf-8"))


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
        for key in INSURANCE_GT_KEYS:
            if row.get(key) not in (None, ""):
                labels[filename][key] = row.get(key)
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
        "session_id", "run_id", "dataset", "split", "mode", "samples", "metrics",
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
    expected_fields = expected_fields_for_sample(sample)
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
    extraction = score_row_extraction(extracted, expected_fields, hf_class)
    row = {
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
    if extraction:
        row["extraction_overall_score"] = extraction["overall_score"]
        row["extraction_n_fields"] = extraction["n_fields"]
        row["extraction_needs_judge_review"] = extraction["needs_judge_review"]
    return row


def _plan_entry(sample: dict) -> dict:
    return {
        "filename": sample.get("filename"),
        "expected_hf_class": sample.get("expected_hf_class"),
        "expected_subclass": sample.get("expected_subclass") or "",
        "chars": sample.get("chars"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--mock", action="store_true")
    mode.add_argument(
        "--finalize",
        metavar="REPORT",
        help="Rewrite metrics + report.md for an existing HF report (no LLM).",
    )
    parser.add_argument("--per-class", type=int, default=1)
    parser.add_argument("--split", default="train")
    parser.add_argument("--max-chars", type=int, default=25000)
    parser.add_argument("--target-chars", type=int, default=6000)
    parser.add_argument("--max-scan", type=int, default=1500)
    parser.add_argument(
        "--resume",
        metavar="REPORT",
        help="Continue an interrupted run from report.json (or its directory). "
             "Skips filenames that already have a non-error stage.",
    )
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

    if args.finalize:
        report = finalize_report(Path(args.finalize))
        metrics = report.get("metrics") or {}
        print(json.dumps({
            "finalized": str(Path(args.finalize)),
            "n": report.get("n"),
            "errors": report.get("errors", 0),
            "metrics": metrics,
        }, default=str))
        return 0 if not report.get("errors") else 1

    from pipeline.env import default_environment, load_env
    from pipeline.logging import setup_logging
    from pipeline.docclass_mode import docclass_prompts_enabled

    load_env()
    default_environment("pilot")
    setup_logging()
    from observability.tracing import ensure_process_tracing, flush as tracing_flush

    ensure_process_tracing()
    os.environ.setdefault("MAILROOM_VISION_ENABLED", "0")
    # Embeddings on every CUAD/MAUD clause burn OpenRouter quota at corpus
    # scale; lexical scoring still runs. Opt back in with MAILROOM_FIELD_SCORING_EMBEDDING=1.
    os.environ.setdefault("MAILROOM_FIELD_SCORING_EMBEDDING", "0")
    if os.environ.get("MAILROOM_FIELD_SCORING_EMBEDDING", "0").lower() in ("0", "false", "no"):
        try:
            from llm_dojo_scoring import configure

            configure(field_scoring__embedding_enabled=False)
        except Exception:
            pass

    mock_mode = bool(args.mock)
    if not mock_mode:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key or key == "mock-key":
            parser.error("OPENROUTER_API_KEY is not set to a real key — refusing --real")

    import scripts.run_pilot as rp

    resume_report: dict = {}
    if args.resume:
        resume_report = _load_resume(Path(args.resume))

    stamp = (
        str(resume_report.get("run_id") or "")
        or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    session_id = str(resume_report.get("session_id") or f"pilot-hf-{stamp}")
    run_id = stamp
    run_matter = str(resume_report.get("matter_id") or f"hf-docclass-merged-{stamp}")
    unique_matters = (
        bool(resume_report["unique_matters"])
        if resume_report and "unique_matters" in resume_report
        else (not args.shared_matter)
    )

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

    if resume_report.get("plan"):
        by_name = {s.get("filename"): s for s in samples}
        ordered = []
        for item in resume_report["plan"]:
            match = by_name.get(item.get("filename"))
            if match:
                ordered.append(match)
        if ordered:
            samples = ordered

    rows = list(resume_report.get("samples") or [])
    samples_to_run = remaining_samples(samples, rows) if rows else list(samples)
    planned_n = len(samples)
    errors = int(resume_report.get("errors") or 0)

    default_abort = max(2.0, 0.04 * max(planned_n, 1))
    abort = float(os.environ.get("MAILROOM_PILOT_COST_ABORT", str(default_abort)))
    rp._COST_ABORT_USD = abort
    rp._COST_WARN_USD = abort * 0.75
    spent = sum(float(r.get("llm_cost_usd") or 0) for r in rows)
    rp._RUN_COST_USD["value"] = spent
    rp._RUN_COST_USD["warned"] = spent >= rp._COST_WARN_USD

    used_names: set[str] = set()
    used_matters: set[str] = set()
    for row in rows:
        if row.get("local_filename"):
            used_names.add(str(row["local_filename"]))
        if row.get("matter_id"):
            used_matters.add(str(row["matter_id"]))

    if args.resume:
        out_dir = Path(args.resume)
        if out_dir.is_file():
            out_dir = out_dir.parent
    else:
        out_dir = _report_root() / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = [_plan_entry(s) for s in samples]
    gap = float(os.environ.get("MAILROOM_HF_PILOT_GAP_S", "0" if mock_mode else "1.5"))

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
            "cost_abort_usd": abort,
            "plan": plan,
            "samples": rows,
            "n": len(rows),
            "planned_n": planned_n,
            "errors": errors,
            "metrics": summarize_rows(rows),
        }
        return write_report_files(out_dir / "report.json", report)

    _flush_report()

    for sample in samples_to_run:
        if gap > 0 and rows:
            time.sleep(gap)
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
            if "cost cap reached" in str(exc).lower():
                errors += 1
                rows.append({
                    "filename": sample.get("filename"),
                    "local_filename": local_name,
                    "matter_id": matter_id,
                    "expected": sample.get("expected_hf_class"),
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
                raise
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
        path = _flush_report()
        metrics = summarize_rows(rows)
        print(json.dumps({
            "progress": f"{len(rows)}/{planned_n}",
            "session_id": session_id,
            "report": str(path),
            "n": len(rows),
            "errors": errors,
            "unique_matters": unique_matters,
            "metrics": {
                "exact_accuracy": metrics.get("exact_accuracy"),
                "aligned_accuracy": metrics.get("aligned_accuracy"),
                "subclass_accuracy": metrics.get("subclass_accuracy"),
                "total_cost_usd": metrics.get("total_cost_usd"),
                "total_tokens": metrics.get("total_tokens"),
                "stages": metrics.get("stages"),
            },
        }), flush=True)

    path = _flush_report()
    metrics = summarize_rows(rows)
    print(json.dumps({
        "session_id": session_id,
        "run_id": run_id,
        "report": str(path),
        "markdown": str(path.with_suffix(".md")),
        "n": len(rows),
        "planned_n": planned_n,
        "errors": errors,
        "docclass_prompts": docclass_prompts_enabled(),
        "unique_matters": unique_matters,
        "metrics": metrics,
    }, default=str))
    try:
        tracing_flush()
    except Exception:
        pass
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
