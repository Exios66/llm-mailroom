#!/usr/bin/env python3
"""Hugging Face corpus pilot — the runner The-Mailroom orchestrates.

Default corpus is ``Lucius-Morningstar/docclass-merged`` schema **v5** (the
targeted full 1,210-doc surface). Class × subtype examples come from
``docclass-pilot``. Any other pipeline-ready Lucius-Morningstar dataset
(``--dataset enron`` / ``claims`` / ``cuad``) can be ingested the same way,
including the 247k-row Enron correspondence corpus.

``scripts/run_production_pilot.py`` in The-Mailroom looks for this file and
invokes ``--check`` / ``--real --per-class N``. Traces land in Langfuse under
session ``pilot-hf-<UTC stamp>`` with tags ``mailroom``, ``pilot``, and the
corpus ``source-*`` tag (plus ``docclass-prompts`` when that arm is on).

  --check     network-free contract (intake + scorer mapping + report schema)
  --mock      pipeline machinery on committed Hub class×subtype examples (fake LLM)
  --real      live Qwen via OpenRouter on a stratified HF subset
  --examples  use docclass-pilot (every class × subclass stratum)
  --dataset   Lucius-Morningstar slug (merged / pilot / enron / claims / cuad)
  --docclass  opt-in KANBAN-090 docclass prompt variants for every agent

Usage:
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --check
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --mock --per-class 1
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --mock --per-subclass 1
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 1
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --examples
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --dataset enron --per-subclass 1 --max-scan 8000
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 5 --docclass --max-scan 4000
    PYTHONPATH=src python src/scripts/run_hf_pilot.py --finalize data/hf_pilot/<stamp>
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
    COMPLIANCE_GT_KEYS,
    CORPORATE_GT_KEYS,
    CORRESPONDENCE_GT_KEYS,
    INSURANCE_GT_KEYS,
    coerce_gt_value,
    normalize_claim_type,
    normalize_communication_type,
    normalize_filing_type,
    normalize_record_type,
)
from pipeline.hf_corpora import (  # noqa: E402
    FULL_CORPUS_ID,
    FULL_CORPUS_REVISION,
    FULL_CORPUS_SCHEMA,
    HUB_CLASSES,
    active_corpus,
    adapt_hub_row,
    example_rows,
    examples_by_class,
    hub_sample,
    pipeline_corpora,
    resolve_corpus,
    set_active_corpus,
)

DATASET_ID = FULL_CORPUS_ID
DATASET_REVISION = FULL_CORPUS_REVISION
DATASET_SCHEMA = FULL_CORPUS_SCHEMA
VIEWER_BASE = "https://datasets-server.huggingface.co"
HF_CLASSES = HUB_CLASSES
# Zero-row / retired classes are scored by dojo suites but MUST NOT appear in
# HF_CLASSES — compliance_filing has zero Hub rows; court/DD were retired.
# A local mock/check pack still scores compliance (and insurance contrast /
# corporate schema extraction) without inventing Hub accuracy.
HF_HONESTY_EXCLUDED = (
    "compliance_filing",
    "court_opinion",
    "due_diligence",
)
HF_LOCAL_PACK_CLASSES = (
    "compliance_filing",
)
# Live taxonomy files MAUD merger rows as merger_agreement (not contract).
# Exact class match is the only class KPI. Do not import
# llm_dojo_scoring.mailroom.align_doc_type (v0.11.0 still maps MAUD ≡ CUAD).
ALIGN: dict[str, str] = {}


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
    extra_keys = CORPORATE_GT_KEYS + COMPLIANCE_GT_KEYS + CORRESPONDENCE_GT_KEYS
    for key in extra_keys:
        if key in sample:
            continue
        raw = gt.get(key)
        if raw in (None, ""):
            raw = row.get(key)
        if raw not in (None, ""):
            sample[key] = coerce_gt_value(raw)
    for key in ("content_topic", "sentiment_label", "maud_clause_labels"):
        raw = gt.get(key)
        if raw in (None, ""):
            raw = row.get(key)
        if raw not in (None, ""):
            sample[key] = coerce_gt_value(raw)
    # Legacy Hub columns → pared semantic enrichment fields.
    legacy_provisions = coerce_gt_value(
        gt.get("key_provisions")
        if gt.get("key_provisions") not in (None, "")
        else row.get("key_provisions")
    )
    if isinstance(legacy_provisions, list) and legacy_provisions:
        sample.setdefault("subject_matter", str(legacy_provisions[0])[:240])
        sample.setdefault(
            "keywords",
            [" ".join(str(p).split()[:4]) for p in legacy_provisions[:8]],
        )
        sample.setdefault("intent", "record_governance")
    legacy_points = coerce_gt_value(
        gt.get("key_points")
        if gt.get("key_points") not in (None, "")
        else row.get("key_points")
    )
    if isinstance(legacy_points, list) and legacy_points:
        sample.setdefault("subject_matter", str(legacy_points[0])[:240])
        sample.setdefault(
            "keywords",
            [" ".join(str(p).split()[:4]) for p in legacy_points[:8]],
        )
        sample.setdefault("intent", "correspondence")
    return sample


def select_stratified(
    rows: list[dict],
    *,
    per_class: int,
    max_chars: int,
    target_chars: int,
    classes: tuple[str, ...] = HF_CLASSES,
    per_subclass: int = 0,
) -> list[dict]:
    """Pick Hub docs by class, or by class × subclass when ``per_subclass`` > 0.

    Oversized docs stay in the pool (MAUD mergers are 100k–1M chars).
    ``max_chars`` truncates the text at run time, not the sample set.
    """
    def _keep(row: dict) -> bool:
        cls = row.get("expected_hf_class")
        if cls not in classes:
            return False
        return int(row.get("chars") or 0) >= 200

    kept = [row for row in rows if _keep(row)]
    selected: list[dict] = []
    if per_subclass and per_subclass > 0:
        buckets: dict[tuple[str, str], list[dict]] = {}
        for row in kept:
            key = (
                str(row.get("expected_hf_class") or ""),
                str(row.get("expected_subclass") or "").strip() or "_",
            )
            buckets.setdefault(key, []).append(row)
        for key in sorted(buckets):
            cands = list(buckets[key])
            cands.sort(key=lambda r: abs(int(r["chars"]) - target_chars))
            selected.extend(cands[:per_subclass])
        return selected
    buckets_cls: dict[str, list[dict]] = {c: [] for c in classes}
    for row in kept:
        buckets_cls[row["expected_hf_class"]].append(row)
    for cls in classes:
        cands = list(buckets_cls[cls])
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
    """Score Hub ``expected_subclass`` against sorter ``doc_subclass`` / extraction.

    Returns None when there is no subclass ground truth to score.
    """
    want = str(expected_subclass or "").strip()
    if not want:
        return None
    extracted = extracted or {}
    hf_class = str(expected_class or "")
    # Sorter doc_subclass is the primary predicted token; extraction fields
    # remain a fallback for older reports that only stored contract_subtype.
    predicted = predicted_subtype or ""
    if hf_class == "contract":
        from langchain_agents.sorter_agent import equivalent_subtypes, normalize_subtype

        got = normalize_subtype(predicted or extracted.get("cuad_family") or extracted.get("contract_subtype"))
        need = normalize_subtype(want)
        return equivalent_subtypes(got, need)
    if hf_class == "merger_agreement":
        from langchain_agents.doc_inventories import normalize_sorter_subclass

        got = (
            normalize_sorter_subclass("merger_agreement", predicted)
            or normalize_consideration(extracted.get("merger_consideration"))
            or infer_merger_consideration(extracted)
            or normalize_consideration(predicted)
        )
        need = normalize_sorter_subclass("merger_agreement", want) or normalize_consideration(want)
        if not got or not need:
            return False
        return got == need
    if hf_class == "corporate_record":
        from langchain_agents.doc_inventories import normalize_sorter_subclass

        got = (
            normalize_sorter_subclass(hf_class, predicted)
            or normalize_record_type(extracted.get("record_type") or predicted)
        )
        need = normalize_sorter_subclass(hf_class, want) or normalize_record_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("record_type") or predicted, want)
    if hf_class == "insurance_claim":
        from langchain_agents.doc_inventories import normalize_sorter_subclass

        got = (
            normalize_sorter_subclass(hf_class, predicted)
            or normalize_claim_type(
                extracted.get("claim_type") or predicted or extracted.get("record_type")
            )
        )
        need = normalize_sorter_subclass(hf_class, want) or normalize_claim_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("claim_type") or predicted, want)
    if hf_class == "correspondence":
        from langchain_agents.doc_inventories import normalize_sorter_subclass

        got = (
            normalize_sorter_subclass(hf_class, predicted)
            or normalize_communication_type(
                extracted.get("communication_type") or predicted
            )
        )
        need = normalize_sorter_subclass(hf_class, want) or normalize_communication_type(want)
        if got and need:
            return got == need
        return _loose_label_match(
            extracted.get("communication_type") or predicted, want
        )
    if hf_class == "compliance_filing":
        from langchain_agents.doc_inventories import normalize_sorter_subclass

        got = (
            normalize_sorter_subclass(hf_class, predicted)
            or normalize_filing_type(extracted.get("filing_type") or predicted)
        )
        need = normalize_sorter_subclass(hf_class, want) or normalize_filing_type(want)
        if got and need:
            return got == need
        return _loose_label_match(extracted.get("filing_type") or predicted, want)
    return _loose_label_match(predicted, want)


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
    """Exact class / subclass accuracy plus cost and stage mix.

    ``aligned_accuracy`` is a deprecated JSON alias of exact (The-Mailroom
    readers). It is not a merger≡contract score — MAUD is its own class.
    """
    from collections import Counter

    from observability.classification_scoring import classes_match, score_exact_classification

    def _row_exact(row: dict) -> bool:
        if row.get("predicted") not in (None, ""):
            return classes_match(row.get("expected"), row.get("predicted"))
        return bool(row.get("exact_ok"))

    n = len(rows)
    class_scores = score_exact_classification(
        [r.get("expected") for r in rows],
        [
            r.get("predicted") if r.get("predicted") not in (None, "") else (
                r.get("expected") if r.get("exact_ok") else ""
            )
            for r in rows
        ],
    )
    subclass_scored = [r for r in rows if r.get("subclass_ok") is not None]
    subclass_n = sum(1 for r in subclass_scored if r.get("subclass_ok"))
    costs = [float(r["llm_cost_usd"]) for r in rows if isinstance(r.get("llm_cost_usd"), (int, float))]
    tokens = [int(r["llm_tokens"]) for r in rows if isinstance(r.get("llm_tokens"), (int, float))]
    calls = [int(r["llm_calls"]) for r in rows if isinstance(r.get("llm_calls"), (int, float))]
    walls = [float(r["wall_time_s"]) for r in rows if isinstance(r.get("wall_time_s"), (int, float))]
    per_class: dict[str, dict] = {}
    per_specialist: dict[str, dict] = {}
    per_subclass: dict[tuple[str, str], dict] = {}
    from observability.specialist_suites import specialist_for_class

    for row in rows:
        cls = row.get("expected") or "unknown"
        specialist = row.get("specialist") or specialist_for_class(cls) or "unknown"
        sub = str(row.get("expected_subclass") or "").strip() or "_"
        exact = _row_exact(row)
        bucket = per_class.setdefault(cls, {
            "n": 0, "exact": 0, "subclass": 0, "subclass_n": 0,
            "cost_usd": 0.0, "tokens": 0, "tokens_known": False, "stages": Counter(),
            "extract_scores": [], "extract_f1": [], "gt_fields": [],
            "specialist": specialist,
        })
        bucket["n"] += 1
        bucket["exact"] += int(exact)
        if row.get("subclass_ok") is not None:
            bucket["subclass_n"] += 1
            bucket["subclass"] += int(bool(row.get("subclass_ok")))
        bucket["cost_usd"] += float(row.get("llm_cost_usd") or 0)
        if isinstance(row.get("llm_tokens"), (int, float)):
            bucket["tokens"] += int(row.get("llm_tokens") or 0)
            bucket["tokens_known"] = True
        bucket["stages"][row.get("stage") or "unknown"] += 1
        if isinstance(row.get("extraction_overall_score"), (int, float)):
            bucket["extract_scores"].append(float(row["extraction_overall_score"]))
        if isinstance(row.get("extraction_f1"), (int, float)):
            bucket["extract_f1"].append(float(row["extraction_f1"]))
        if isinstance(row.get("extraction_gt_n_fields"), (int, float)):
            bucket["gt_fields"].append(int(row["extraction_gt_n_fields"]))
        spec = per_specialist.setdefault(specialist, {
            "n": 0, "classes": set(), "extract_scores": [], "extract_f1": [],
            "gt_fields": [],
        })
        spec["n"] += 1
        spec["classes"].add(cls)
        if isinstance(row.get("extraction_overall_score"), (int, float)):
            spec["extract_scores"].append(float(row["extraction_overall_score"]))
        if isinstance(row.get("extraction_f1"), (int, float)):
            spec["extract_f1"].append(float(row["extraction_f1"]))
        if isinstance(row.get("extraction_gt_n_fields"), (int, float)):
            spec["gt_fields"].append(int(row["extraction_gt_n_fields"]))
        stratum = per_subclass.setdefault((cls, sub), {
            "n": 0, "exact": 0, "subclass": 0, "subclass_n": 0,
        })
        stratum["n"] += 1
        stratum["exact"] += int(exact)
        if row.get("subclass_ok") is not None:
            stratum["subclass_n"] += 1
            stratum["subclass"] += int(bool(row.get("subclass_ok")))
    scores = [
        float(r["extraction_overall_score"])
        for r in rows
        if isinstance(r.get("extraction_overall_score"), (int, float))
    ]
    quality_dc = [
        float(r["determination_consistency"])
        for r in rows
        if isinstance(r.get("determination_consistency"), (int, float))
        and r.get("determination_consistency_is_quality") is not False
        and not r.get("gt_homogeneity")
    ]
    gated_dc = [
        float(r["determination_consistency"])
        for r in rows
        if isinstance(r.get("determination_consistency"), (int, float))
        and (r.get("gt_homogeneity") or r.get("determination_consistency_is_quality") is False)
    ]
    extra_keys = (
        "maud_question_accuracy",
        "maud_question_macro_accuracy",
        "content_topic_accuracy",
        "sentiment_accuracy",
        "extraction_f1",
        "extraction_precision",
        "extraction_recall",
        "entity_list_f1",
    )
    extra_means: dict[str, float] = {}
    for key in extra_keys:
        vals = [
            float(r[key]) for r in rows
            if isinstance(r.get(key), (int, float))
        ]
        if vals:
            extra_means[f"{key}_mean"] = round(sum(vals) / len(vals), 3)
            extra_means[f"{key}_n"] = len(vals)
    out = {
        "n": n,
        "exact_n": class_scores["exact_n"],
        "aligned_n": class_scores["aligned_n"],
        "exact_accuracy": class_scores["exact_accuracy"],
        "aligned_accuracy": class_scores["aligned_accuracy"],
        "aligned_equals_exact": True,
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
                "aligned": v["exact"],
                "exact_accuracy": round(v["exact"] / v["n"], 3) if v["n"] else 0.0,
                "aligned_accuracy": round(v["exact"] / v["n"], 3) if v["n"] else 0.0,
                "subclass_accuracy": (
                    round(v["subclass"] / v["subclass_n"], 3) if v["subclass_n"] else None
                ),
                "cost_usd": round(v["cost_usd"], 6),
                "tokens": v["tokens"] if v["tokens_known"] else None,
                "stages": dict(v["stages"]),
                "specialist": v.get("specialist"),
                "extraction_n": len(v["extract_scores"]),
                "extraction_overall_mean": (
                    round(sum(v["extract_scores"]) / len(v["extract_scores"]), 3)
                    if v["extract_scores"] else None
                ),
                "extraction_f1_mean": (
                    round(sum(v["extract_f1"]) / len(v["extract_f1"]), 3)
                    if v["extract_f1"] else None
                ),
                "extraction_gt_fields_mean": (
                    round(sum(v["gt_fields"]) / len(v["gt_fields"]), 2)
                    if v["gt_fields"] else None
                ),
            }
            for cls, v in sorted(per_class.items())
        },
        "per_specialist": {
            name: {
                "n": v["n"],
                "classes": sorted(v["classes"]),
                "extraction_n": len(v["extract_scores"]),
                "extraction_overall_mean": (
                    round(sum(v["extract_scores"]) / len(v["extract_scores"]), 3)
                    if v["extract_scores"] else None
                ),
                "extraction_f1_mean": (
                    round(sum(v["extract_f1"]) / len(v["extract_f1"]), 3)
                    if v["extract_f1"] else None
                ),
                "extraction_gt_fields_mean": (
                    round(sum(v["gt_fields"]) / len(v["gt_fields"]), 2)
                    if v["gt_fields"] else None
                ),
            }
            for name, v in sorted(per_specialist.items())
        },
        "per_subclass": {
            f"{cls}/{sub}": {
                "n": v["n"],
                "exact_accuracy": round(v["exact"] / v["n"], 3) if v["n"] else 0.0,
                "subclass_accuracy": (
                    round(v["subclass"] / v["subclass_n"], 3) if v["subclass_n"] else None
                ),
            }
            for (cls, sub), v in sorted(per_subclass.items())
        },
    }
    if scores:
        out["extraction_n"] = len(scores)
        out["extraction_overall_mean"] = round(sum(scores) / len(scores), 3)
    if quality_dc:
        out["determination_consistency_n"] = len(quality_dc)
        out["determination_consistency_mean"] = round(sum(quality_dc) / len(quality_dc), 3)
    if gated_dc:
        out["determination_consistency_gated_n"] = len(gated_dc)
        out["determination_consistency_gated_mean"] = round(sum(gated_dc) / len(gated_dc), 3)
    out.update(extra_means)
    return out


def hf_corpus_honesty() -> dict:
    """Per-class corpus honesty from the dedicated specialist suites.

    Includes scored HF_CLASSES plus the zero-row/retired exclusions so a
    report never invents compliance accuracy at n=0. Local packs are
    attached as extras (mock/check only) — they do not flip ``in_hf_pilot``.
    """
    from observability.honest_gaps import suite_honesty
    from observability.local_eval_packs import local_pack_status

    out: dict[str, dict] = {}
    for cls in (*HF_CLASSES, *HF_HONESTY_EXCLUDED):
        payload = suite_honesty(cls)
        payload["in_hf_pilot"] = cls in HF_CLASSES
        payload.update(local_pack_status(cls))
        out[cls] = payload
    return out


def expected_fields_for_sample(sample: dict) -> dict:
    """Scorable specialist-schema labels: Hub catalog first, post-hoc fill.

    Official Hub / explicit ``expected_fields`` always win. Remaining
    schema fields are parsed from document text so every live class can be
    scored, not only CUAD/MAUD contracts.
    """
    from observability.extraction_gt import build_expected_fields

    fields, _meta = build_expected_fields(sample)
    return fields


def expected_fields_meta(sample: dict) -> dict:
    """Provenance for one sample's extraction GT (hub vs post-hoc)."""
    from observability.extraction_gt import build_expected_fields

    _fields, meta = build_expected_fields(sample)
    return meta


def score_row_extraction(extracted: dict | None, expected_fields: dict | None, doc_class: str) -> dict | None:
    """Deterministic field score via the dedicated specialist suite."""
    if not expected_fields or not extracted:
        return None
    try:
        from observability.field_scoring import get_field_types
        from observability.suite_scoring import score_with_suite

        scored_class = doc_class
        suite_class = scored_class
        result, extras = score_with_suite(
            suite_class,
            extracted,
            expected_fields,
            field_types=get_field_types(scored_class),
        )
        overall = result.overall_score
        out = {
            "overall_score": None if overall is None else round(float(overall), 3),
            "n_fields": len(result.field_scores or {}),
            "needs_judge_review": bool(result.ambiguous_fields),
        }
        for key, value in extras.items():
            out[key] = round(float(value), 3)
        if doc_class == "insurance_claim":
            from observability.honest_gaps import (
                determination_consistency_is_quality,
                insurance_determination_consistent,
                insurance_gt_is_homogeneous,
            )

            consistent = insurance_determination_consistent(extracted)
            if consistent is not None:
                # Local invariant, not a registered dojo extra.
                out["local_determination_consistent"] = consistent
            if insurance_gt_is_homogeneous(expected_fields):
                out["gt_homogeneity"] = True
            if not determination_consistency_is_quality(expected_fields):
                out["determination_consistency_is_quality"] = False
        return out
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
        if catalog.get("doc_subclass") and not out.get("predicted_subtype"):
            out["predicted_subtype"] = catalog["doc_subclass"]
        if catalog.get("contract_subtype") and not out.get("predicted_subtype"):
            out["predicted_subtype"] = catalog["contract_subtype"]
    extracted = out.get("extracted_data") or {}
    if out.get("predicted") not in (None, ""):
        from observability.classification_scoring import classes_match

        exact = classes_match(out.get("expected"), out.get("predicted"))
        out["exact_ok"] = exact
        out["aligned_ok"] = exact
    if out.get("subclass_ok") is None and out.get("expected"):
        out["subclass_ok"] = subclass_ok(
            str(out.get("expected") or ""),
            str(out.get("expected_subclass") or ""),
            predicted_subtype=str(out.get("predicted_subtype") or ""),
            extracted=extracted if isinstance(extracted, dict) else {},
        )
    expected_fields = out.get("expected_fields")
    if not isinstance(expected_fields, dict) or not expected_fields:
        payload = {
            "expected_hf_class": out.get("expected"),
            "expected_subclass": out.get("expected_subclass"),
            "text": out.get("text") or "",
            "cuad_clauses": out.get("cuad_clauses") or [],
            "maud_clauses": out.get("maud_clauses") or [],
            **{k: out.get(k) for k in ("content_topic", "sentiment_label", "maud_clause_labels") if out.get(k) not in (None, "")},
            **{k: out.get(k) for k in INSURANCE_GT_KEYS if out.get(k) not in (None, "")},
            **{k: out.get(k) for k in CORPORATE_GT_KEYS if out.get(k) not in (None, "")},
            **{k: out.get(k) for k in COMPLIANCE_GT_KEYS if out.get(k) not in (None, "")},
            **{k: out.get(k) for k in CORRESPONDENCE_GT_KEYS if out.get(k) not in (None, "")},
        }
        expected_fields = expected_fields_for_sample(payload)
        meta = expected_fields_meta(payload)
        out["extraction_gt_n_fields"] = meta.get("n_fields")
        out["extraction_gt_n_posthoc"] = meta.get("n_posthoc")
        out["specialist"] = meta.get("specialist")
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
    for key, label in (
        ("maud_question_accuracy_mean", "MAUD question accuracy"),
        ("content_topic_accuracy_mean", "correspondence topic accuracy"),
        ("extraction_f1_mean", "extraction F1"),
    ):
        if metrics.get(key) is not None:
            n_key = key.replace("_mean", "_n")
            lines.append(
                f"- {label} mean = **{metrics[key]}** over {metrics.get(n_key)} docs"
            )
    if metrics.get("determination_consistency_mean") is not None:
        lines.append(
            f"- determination_consistency (mixed-GT quality) mean = "
            f"**{metrics['determination_consistency_mean']}** over "
            f"{metrics.get('determination_consistency_n')} docs"
        )
    if metrics.get("determination_consistency_gated_n"):
        lines.append(
            f"- determination_consistency on homogeneous CMS GT is **gated** "
            f"(n={metrics['determination_consistency_gated_n']}, "
            f"mean={metrics.get('determination_consistency_gated_mean')} — not a quality KPI)"
        )
    honesty = report.get("honesty") or hf_corpus_honesty()
    lines += [
        "",
        "## Corpus honesty (dojo 0.11.0)",
        "",
        "Gaps are suite metadata, not invented accuracy. `compliance_filing` stays "
        "out of Hub `--real` (zero Hub rows) and is scored by a **local pack** "
        "(mock/check only). `court_opinion` / `due_diligence` are retired. "
        "`corporate_record` Hub official GT is still subclass-only; post-hoc "
        "schema labels parsed from the S-1/exhibit text are now scored. "
        "Insurance `determination_consistency` "
        "is a registered scorer; CMS Hub GT is homogeneous (all-approved), so "
        "that extra is gated on Hub rows and exercised on the local contrast pack. "
        "Every live specialist has a dedicated scoring suite; merger shares the "
        "contracts *agent* but keeps its own MAUD suite.",
        "",
        "| class | in HF pilot | in_corpus | retired | local pack | honest gap |",
        "|---|---|---|---|---|---|",
    ]
    for cls, payload in honesty.items():
        gap = payload.get("honest_gap") or "—"
        if len(str(gap)) > 140:
            gap = str(gap)[:137] + "…"
        local = payload.get("local_pack") or "—"
        lines.append(
            f"| {cls} | {payload.get('in_hf_pilot')} | {payload.get('in_corpus')} | "
            f"{payload.get('retired')} | {local} | {gap} |"
        )
    local_packs = report.get("local_packs")
    if local_packs:
        lines += [
            "",
            "## Local eval packs (mock/check; not Hub accuracy)",
            "",
            "Perfect-extract scores are scorer self-checks on committed fixtures.",
            "",
        ]
        for name, pack in local_packs.items():
            perfect = pack.get("perfect_extract") or {}
            lines.append(
                f"- **{name}** (`{pack.get('doc_class')}`, n={pack.get('n')}): "
                f"overall={perfect.get('extraction_overall_mean')} "
                f"f1={perfect.get('extraction_f1_mean')}"
            )
            if name == "insurance_contrast":
                adv = pack.get("adversarial_denied_without_reasons") or {}
                lines.append(
                    f"  - determinations={pack.get('determinations')} "
                    f"gt_homogeneity={pack.get('gt_homogeneity')} "
                    f"adversarial denied-without-reasons consistency="
                    f"{adv.get('determination_consistency')}"
                )
            if name == "compliance_filing":
                lines.append(
                    f"  - Hub rows=0; subclasses={pack.get('subclasses')}"
                )
            if name == "corporate_extraction":
                lines.append(
                    f"  - Hub extract is subclass-only; local schema fields="
                    f"{pack.get('schema_fields')}"
                )
    stages = metrics.get("stages") or {}
    if stages:
        mix = ", ".join(f"{k}={v}" for k, v in sorted(stages.items()))
        lines += ["", f"- stages: {mix}"]
    lines += [
        "",
        "## Per class",
        "",
        "| class | specialist | n | exact | subclass | extract n | extract overall | extract F1 | GT fields |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cls, stats in (metrics.get("per_class") or {}).items():
        lines.append(
            f"| {cls} | {stats.get('specialist') or ''} | {stats.get('n')} | "
            f"{stats.get('exact_accuracy')} | {stats.get('subclass_accuracy')} | "
            f"{stats.get('extraction_n')} | {stats.get('extraction_overall_mean')} | "
            f"{stats.get('extraction_f1_mean')} | {stats.get('extraction_gt_fields_mean')} |"
        )
    if metrics.get("per_specialist"):
        lines += [
            "",
            "## Specialist extraction suites",
            "",
            "One dedicated suite per live specialist. `contracts_specialist` "
            "extracts both CUAD `contract` and MAUD `merger_agreement`; each "
            "class still has its own suite (CUAD families vs MAUD extras).",
            "",
            "| specialist | classes | n | extract n | overall | F1 | GT fields mean |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for name, stats in (metrics.get("per_specialist") or {}).items():
            classes = ", ".join(stats.get("classes") or [])
            lines.append(
                f"| {name} | {classes} | {stats.get('n')} | "
                f"{stats.get('extraction_n')} | {stats.get('extraction_overall_mean')} | "
                f"{stats.get('extraction_f1_mean')} | {stats.get('extraction_gt_fields_mean')} |"
            )
    if metrics.get("per_subclass"):
        lines += [
            "",
            "## Per subclass (Hub class × subtype strata)",
            "",
            "Strata come from the v5 Hub inventories (`docclass-pilot` / "
            "`docclass-merged`). Predicting `contract` for a `merger_agreement` "
            "row is a class miss, not an aligned hit.",
            "",
            "| stratum | n | exact | subclass |",
            "|---|---:|---:|---:|",
        ]
        for name, stats in (metrics.get("per_subclass") or {}).items():
            lines.append(
                f"| {name} | {stats.get('n')} | {stats.get('exact_accuracy')} | "
                f"{stats.get('subclass_accuracy')} |"
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
    payload["honesty"] = hf_corpus_honesty()
    try:
        from observability.local_eval_packs import score_local_packs

        payload["local_packs"] = score_local_packs()
    except Exception:
        payload.setdefault("local_packs", report.get("local_packs") or {})
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
                "SELECT extracted_data, contract_subtype, original_filename, doc_subclass "
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
        "doc_subclass": (row[3] if len(row) > 3 else "") or "",
    }


def _mock_samples(per_class: int, *, per_subclass: int = 0) -> list[dict]:
    """Hub class×subtype examples from the committed docclass-pilot snapshot.

    Invented Acme/Beta stand-ins are not used — every mock document is a
    truncated Hub row. Local eval packs still append compliance (zero Hub
    rows) and honesty-gap contrast samples.
    """
    out: list[dict] = []
    if per_subclass and per_subclass > 0:
        buckets: dict[tuple[str, str], int] = {}
        for row in example_rows():
            key = (
                str(row.get("expected") or ""),
                str(row.get("expected_subclass") or "").strip() or "_",
            )
            n = buckets.get(key, 0)
            if n >= per_subclass:
                continue
            sample = hub_sample(row)
            if sample["expected_hf_class"] not in HF_CLASSES:
                continue
            if n:
                stem = Path(sample["filename"]).stem
                suff = Path(sample["filename"]).suffix or ".txt"
                sample["filename"] = f"{stem}__{n}{suff}"
            out.append(sample)
            buckets[key] = n + 1
    else:
        for hf_class, row in examples_by_class().items():
            if hf_class not in HF_CLASSES:
                continue
            sample = hub_sample(row)
            for i in range(max(1, per_class)):
                item = dict(sample)
                if i:
                    stem = Path(sample["filename"]).stem
                    suff = Path(sample["filename"]).suffix or ".txt"
                    item["filename"] = f"{stem}_{i}{suff}"
                out.append(item)
    try:
        from observability.local_eval_packs import all_local_pack_samples

        out.extend(all_local_pack_samples())
    except Exception:
        pass
    return out


def _viewer_rows(split: str, offset: int, length: int, *, config: str = "default") -> dict:
    import httpx

    corpus = active_corpus()
    params = {
        "dataset": corpus["id"],
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


def _scan_cap(max_scan: int) -> int | None:
    """``max_scan <= 0`` means unlimited (do not use on the 247k Enron set)."""
    if max_scan is None or int(max_scan) <= 0:
        return None
    return int(max_scan)


def _paginate_viewer(*, split: str, max_scan: int, config: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    cap = _scan_cap(max_scan)
    limit = cap if cap is not None else 10**9
    while offset < limit:
        take = 100 if cap is None else min(100, limit - offset)
        if take <= 0:
            break
        payload = _viewer_rows(split, offset, take, config=config)
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
    corpus = active_corpus()
    gt_config = corpus.get("gt_config")
    if not gt_config:
        return {}
    labels: dict[str, dict] = {}
    try:
        from datasets import load_dataset  # type: ignore

        kwargs: dict = {"split": split}
        if corpus.get("revision"):
            kwargs["revision"] = corpus["revision"]
        ds = load_dataset(corpus["id"], gt_config, **kwargs)
        raw_rows = _take_rows(ds, max_scan)
    except Exception:
        raw_rows = _paginate_viewer(split=split, max_scan=max_scan, config=gt_config)
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
            "content_topic": row.get("content_topic"),
            "sentiment_label": row.get("sentiment_label"),
        }
        for key in (*INSURANCE_GT_KEYS, *CORPORATE_GT_KEYS, *COMPLIANCE_GT_KEYS, *CORRESPONDENCE_GT_KEYS):
            if row.get(key) not in (None, ""):
                labels[filename][key] = row.get(key)
    return labels


def _take_rows(ds, max_scan: int) -> list[dict]:
    cap = _scan_cap(max_scan)
    if cap is None:
        return [dict(row) for row in ds]
    return [dict(row) for i, row in enumerate(ds) if i < cap]


def load_hf_rows(*, split: str, max_scan: int) -> list[dict]:
    """Load default-config text rows joined to ground_truth labels on filename."""
    corpus = active_corpus()
    labels = load_ground_truth_labels(split=split, max_scan=max_scan)
    parsed: list[dict] = []
    try:
        from datasets import load_dataset  # type: ignore

        kwargs: dict = {"split": split}
        if corpus.get("revision"):
            kwargs["revision"] = corpus["revision"]
        ds = load_dataset(corpus["id"], **kwargs)
        default_rows = _take_rows(ds, max_scan)
    except Exception:
        default_rows = _paginate_viewer(split=split, max_scan=max_scan, config="default")
    for row in default_rows:
        item = parse_hf_row(adapt_hub_row(row, corpus), labels)
        if item:
            parsed.append(item)
    return parsed


def _trace_source() -> str:
    """Langfuse ``source-*`` tag body for the active Hub corpus."""
    corp = active_corpus()
    tag = str(corp.get("source_tag") or f"source-{corp['slug']}")
    return tag.removeprefix("source-")


def _report_root() -> Path:
    override = os.environ.get("MAILROOM_HF_PILOT_DIR")
    if override:
        return Path(override)
    return REPO_ROOT / "data" / "hf_pilot"


def check_contract() -> int:
    from agents.intake import deterministic_normalize, looks_messy
    from llm_dojo_scoring import get_suite

    cleaned, stats = deterministic_normalize("A\u00a0B\n\n\n\nagree-\nment")
    assert "A B" in cleaned
    assert "agreement" in cleaned
    assert stats["changed"] is True
    assert looks_messy("x\n" * 30) is True
    intake_out = get_suite("intake").score("A\u00a0B\n\n\n\nagree-\nment", cleaned)
    assert intake_out["intake_prep_completeness"] == 1.0
    assert pipeline_class("merger_agreement") == "merger_agreement"
    assert pipeline_class("insurance_claim") == "insurance_claim"
    assert ALIGN == {}
    from observability.classification_scoring import classes_match, score_exact_classification

    assert classes_match("merger_agreement", "contract") is False
    miss = score_exact_classification(["merger_agreement"], ["contract"])
    assert miss["exact_accuracy"] == 0.0
    assert miss["aligned_accuracy"] == 0.0
    assert miss["aligned_equals_exact"] is True
    assert "compliance_filing" not in HF_CLASSES
    assert "compliance_filing" in HF_LOCAL_PACK_CLASSES
    for retired in ("court_opinion", "due_diligence"):
        assert retired not in HF_CLASSES
        assert get_suite(retired).retired is True
    from observability.honest_gaps import (
        determination_consistency_is_quality,
        insurance_determination_consistent,
        insurance_expected_set_is_homogeneous,
        suite_honesty,
    )
    from observability.local_eval_packs import score_local_packs

    insurance = suite_honesty("insurance_claim")
    assert insurance["in_corpus"] is True
    gap = (insurance["honest_gap"] or "").lower()
    assert "homogeneous" in gap or "degenerate" in gap
    assert "determination_consistency" in gap
    compliance = suite_honesty("compliance_filing")
    assert compliance["in_corpus"] is False
    assert "zero" in (compliance["honest_gap"] or "").lower()
    corporate = suite_honesty("corporate_record")
    assert corporate["in_corpus"] is True
    corp_gap = (corporate["honest_gap"] or "").lower()
    assert "external" in corp_gap and "extraction benchmark" in corp_gap
    assert insurance_determination_consistent(
        {"coverage_determination": "approved", "denial_reasons": []}
    ) is True
    assert insurance_determination_consistent(
        {"coverage_determination": "denied", "denial_reasons": []}
    ) is False
    assert insurance_expected_set_is_homogeneous([
        {"coverage_determination": "approved", "denial_reasons": []},
        {"coverage_determination": "approved", "denial_reasons": []},
    ]) is True
    assert insurance_expected_set_is_homogeneous([
        {"coverage_determination": "approved", "denial_reasons": []},
        {"coverage_determination": "denied", "denial_reasons": ["lapse"]},
    ]) is False
    assert determination_consistency_is_quality(
        {"coverage_determination": "approved", "denial_reasons": []}
    ) is False
    packs = score_local_packs()
    contrast = packs["insurance_contrast"]
    assert contrast["gt_homogeneity"] is False
    assert set(contrast["determinations"]) == {"approved", "denied", "partial"}
    assert contrast["perfect_extract"]["determination_consistency_mean"] == 1.0
    assert contrast["adversarial_denied_without_reasons"]["determination_consistency"] == 0.0
    assert contrast["hub_cms_shaped"]["gt_homogeneity"] is True
    assert packs["compliance_filing"]["n"] >= 2
    assert packs["compliance_filing"]["in_hub"] is False
    assert packs["compliance_filing"]["perfect_extract"]["n"] >= 2
    corp_pack = packs["corporate_extraction"]
    assert corp_pack["hub_extract_is_subclass_only"] is True
    assert "entity_name" in corp_pack["schema_fields"]
    assert "subject_matter" in corp_pack["schema_fields"]
    assert corp_pack["perfect_extract"]["n"] >= 2
    honesty = hf_corpus_honesty()
    assert honesty["compliance_filing"]["in_hf_pilot"] is False
    assert honesty["compliance_filing"]["local_pack"] == "compliance_filing"
    assert honesty["corporate_record"]["local_pack"] == "corporate_extraction"
    assert honesty["insurance_claim"]["hub_gt_homogeneous"] is True
    from observability.specialist_suites import (
        LIVE_EXTRACT_CLASSES,
        list_dedicated_suites,
        specialists_with_suites,
    )

    suites = {row["doc_class"]: row for row in list_dedicated_suites()}
    assert set(LIVE_EXTRACT_CLASSES) == set(suites)
    for kind, row in suites.items():
        assert row["specialist"], kind
        assert row["schema_fields"], kind
        assert get_suite(kind).doc_type == kind
    mapping = specialists_with_suites()
    assert mapping["contracts_specialist"] == ["contract", "merger_agreement"]
    assert mapping["corporate_records_specialist"] == ["corporate_record"]
    assert mapping["correspondence_specialist"] == ["correspondence"]
    assert mapping["compliance_specialist"] == ["compliance_filing"]
    assert mapping["insurance_claims_specialist"] == ["insurance_claim"]
    from pipeline.hf_corpora import example_rows, hub_sample

    by_class = {}
    for raw in example_rows():
        sample = hub_sample(raw)
        cls = sample["expected_hf_class"]
        if cls in by_class:
            continue
        fields = expected_fields_for_sample(sample)
        meta = expected_fields_meta(sample)
        by_class[cls] = (fields, meta)
        assert meta["n_fields"] >= 2, (cls, meta)
        assert meta["n_labeled"] >= 2, (cls, fields)
    assert set(by_class) == set(HF_CLASSES)
    cms_fields, cms_meta = by_class["insurance_claim"]
    assert cms_fields.get("insurer")
    assert cms_fields.get("claim_number")
    assert cms_meta["n_posthoc"] >= 1
    corp_fields, _ = by_class["corporate_record"]
    assert corp_fields.get("record_type")
    assert corp_fields.get("entity_name") or corp_fields.get("jurisdiction")
    mail_fields, _ = by_class["correspondence"]
    assert mail_fields.get("communication_type")
    merger_fields, _ = by_class["merger_agreement"]
    assert merger_fields.get("merger_consideration")
    assert merger_fields.get("parties") or merger_fields.get("document_name")
    contract_fields, _ = by_class["contract"]
    assert contract_fields.get("cuad_family")
    from observability.local_eval_packs import compliance_local_samples

    compliance_sample = compliance_local_samples()[0]
    compliance_gt = expected_fields_for_sample(compliance_sample)
    assert compliance_gt.get("filing_type")
    assert compliance_gt.get("entity_name")
    assert DATASET_SCHEMA == "v5"
    assert DATASET_ID == "Lucius-Morningstar/docclass-merged"
    assert DATASET_REVISION
    pack_classes = set(examples_by_class())
    assert pack_classes == set(HF_CLASSES)
    strata = {
        (row["expected"], row.get("expected_subclass") or "")
        for row in example_rows()
    }
    assert len(strata) == 48
    slugs = {c["slug"] for c in pipeline_corpora()}
    assert "enron-correspondence-dedup" in slugs
    assert "cms-desynpuf-insurance-claims" in slugs
    assert resolve_corpus("legalbench-full")["pipeline"] is False
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
        "aligned_equals_exact": True,
        "report_keys": sorted(report_keys),
        "sample_keys": sorted(sample_keys),
        "dataset": DATASET_ID,
        "schema": DATASET_SCHEMA,
        "revision": DATASET_REVISION,
        "example_strata": len(strata),
        "n_classes": len(HF_CLASSES),
        "honesty_excluded": list(HF_HONESTY_EXCLUDED),
        "local_pack_classes": list(HF_LOCAL_PACK_CLASSES),
        "pipeline_datasets": sorted(slugs),
        "corporate_in_corpus": True,
        "compliance_in_corpus": False,
        "local_packs": {
            "insurance_contrast": packs["insurance_contrast"]["n"],
            "compliance_filing": packs["compliance_filing"]["n"],
            "corporate_extraction": packs["corporate_extraction"]["n"],
        },
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
    expect_type = hf_class
    expect = {"doc_type": expect_type, "conf": 0.96}
    ground_truth = {
        "expected": hf_class,
        "expected_doc_class": expect_type,
        "expected_hf_class": hf_class,
    }
    if sample.get("expected_subclass"):
        ground_truth["expected_subclass"] = sample["expected_subclass"]
    expected_fields = expected_fields_for_sample(sample)
    gt_meta = expected_fields_meta(sample)
    if expected_fields:
        ground_truth["expected_fields"] = expected_fields
    if gt_meta.get("sources"):
        ground_truth["expected_fields_sources"] = gt_meta["sources"]

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
                queued, matter_id, source=_trace_source(),
                ground_truth=ground_truth, session_id=session_id, run_id=run_id,
            )
    else:
        with patch("llm.client.get_llm", side_effect=rp._real_get_llm), \
             patch("agents.base.get_llm", side_effect=rp._real_get_llm), \
             patch.object(_LangChainBaseAgent, "llm", new=rp._make_real_langchain_llm()):
            result = run_pipeline(
                queued, matter_id, source=_trace_source(),
                ground_truth=ground_truth, session_id=session_id, run_id=run_id,
            )
    wall = time.perf_counter() - started
    predicted = result.get("doc_type")
    extracted = _public_extracted(result.get("extracted_data"))
    subtype = result.get("doc_subclass") or result.get("contract_subtype") or ""
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
        "aligned_ok": predicted == hf_class,  # deprecated alias of exact_ok
        "subclass_ok": subclass,
        "specialist": gt_meta.get("specialist"),
        "expected_fields": expected_fields,
        "extraction_gt_n_fields": gt_meta.get("n_fields"),
        "extraction_gt_n_posthoc": gt_meta.get("n_posthoc"),
        "extraction_gt_coverage": gt_meta.get("coverage"),
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
        for key, value in extraction.items():
            if key in ("overall_score", "n_fields", "needs_judge_review"):
                continue
            row[key] = value
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
    parser.add_argument(
        "--per-subclass",
        type=int,
        default=0,
        help="Pick N documents per (class, subclass) stratum instead of "
             "per-class. --examples implies --per-subclass 1.",
    )
    parser.add_argument(
        "--dataset",
        default="docclass-merged",
        help="Lucius-Morningstar corpus slug or repo id (default: "
             "docclass-merged v5). Aliases: v5/full, examples/pilot, "
             "enron, claims, cuad.",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="Use docclass-pilot (every class × subclass stratum of v5) "
             "instead of the full merged corpus.",
    )
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

    if args.examples:
        corpus = set_active_corpus("docclass-pilot")
        if args.per_subclass <= 0:
            args.per_subclass = 1
        # The 48-stratum pack is 138 rows; do not clip it at the default --max-scan.
        args.max_scan = 0
    else:
        corpus = set_active_corpus(args.dataset)
    if not corpus.get("pipeline"):
        parser.error(
            f"{corpus['id']} is not a document-pipeline ingest corpus "
            f"({corpus.get('note') or corpus.get('role')})"
        )
    hub_classes = tuple(corpus.get("classes") or HF_CLASSES)

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
    run_matter = str(resume_report.get("matter_id") or f"hf-{corpus['slug']}-{stamp}")
    unique_matters = (
        bool(resume_report["unique_matters"])
        if resume_report and "unique_matters" in resume_report
        else (not args.shared_matter)
    )

    if mock_mode:
        samples = _mock_samples(
            max(1, args.per_class), per_subclass=args.per_subclass,
        )
    else:
        raw = load_hf_rows(split=args.split, max_scan=args.max_scan)
        samples = select_stratified(
            raw,
            per_class=max(1, args.per_class),
            max_chars=args.max_chars,
            target_chars=args.target_chars,
            classes=hub_classes,
            per_subclass=args.per_subclass,
        )
        if args.per_subclass:
            if not samples:
                raise SystemExit(
                    f"HF subset empty for {corpus['id']} split={args.split} "
                    f"per_subclass={args.per_subclass}. Labels must come from "
                    "config=ground_truth joined to default-config doc_text."
                )
        else:
            got = {c: 0 for c in hub_classes}
            for s in samples:
                got[s["expected_hf_class"]] = got.get(s["expected_hf_class"], 0) + 1
            missing = [c for c in hub_classes if got.get(c, 0) < max(1, args.per_class)]
            if missing:
                raise SystemExit(
                    f"HF subset incomplete for {corpus['id']} split={args.split} "
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
            "dataset": corpus["id"],
            "schema": corpus.get("schema"),
            "revision": corpus.get("revision"),
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
                "expected_doc_class": sample.get("expected_hf_class") or "",
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
                "aligned_equals_exact": True,
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
