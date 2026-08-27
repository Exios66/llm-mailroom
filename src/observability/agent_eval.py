"""Per-agent isolation eval — invoke one mailroom agent against labeled cases.

Live Langfuse evaluators remain pipeline-level (``pipeline-result``) by
design. This module is the local methodology for scoring a single agent
without running the 13-node graph.

Cases come from test fixtures, local eval packs, and the live manifest.
``--real`` is gated by ``prepare_samples.is_real_sample`` the same way
``run_pilot.py`` is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
FIXTURES = SRC_DIR / "tests" / "fixtures"
MANIFEST = REPO_ROOT / "docs" / "examples" / "samples" / "manifest.csv"

LLM_AGENTS: tuple[str, ...] = (
    "sorter",
    "sorter_reviewer",
    "contracts_specialist",
    "corporate_records_specialist",
    "correspondence_specialist",
    "compliance_specialist",
    "insurance_claims_specialist",
    "reporter",
    "boss",
    "pdf_transcriber",
    "image_extractor",
    "judge",
    "arbiter",
)

SPECIALIST_FOR_CLASS: dict[str, str] = {
    "contract": "contracts_specialist",
    "merger_agreement": "contracts_specialist",
    "corporate_record": "corporate_records_specialist",
    "correspondence": "correspondence_specialist",
    "compliance_filing": "compliance_specialist",
    "insurance_claim": "insurance_claims_specialist",
}

_LIVE_FIXTURE_CLASSES = frozenset(SPECIALIST_FOR_CLASS)


def token_overlap(predicted: str, expected: str) -> float:
    """SQuAD-style token F1 on two strings (transcription / report checks)."""
    pred = {t for t in (predicted or "").lower().split() if t}
    exp = {t for t in (expected or "").lower().split() if t}
    if not pred and not exp:
        return 1.0
    if not pred or not exp:
        return 0.0
    both = pred & exp
    precision = len(both) / len(pred)
    recall = len(both) / len(exp)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_fixture_cases(doc_class: str | None = None) -> list[dict[str, Any]]:
    """Labeled fixture files for live classes (retired court/DD omitted)."""
    cases: list[dict[str, Any]] = []
    if not FIXTURES.exists():
        return cases
    for folder in sorted(FIXTURES.iterdir()):
        if not folder.is_dir():
            continue
        kind = folder.name
        if kind not in _LIVE_FIXTURE_CLASSES:
            continue
        if doc_class and kind != doc_class:
            continue
        for path in sorted(folder.glob("*.txt")):
            cases.append(
                {
                    "id": f"fixture:{kind}/{path.name}",
                    "filename": path.name,
                    "path": path,
                    "text": _read_text(path),
                    "expected_doc_class": kind,
                    "expected_fields": {},
                    "source": "fixture",
                    "mock_only": True,
                }
            )
    return cases


def load_local_pack_cases(doc_class: str | None = None) -> list[dict[str, Any]]:
    from observability.local_eval_packs import all_local_pack_samples

    cases: list[dict[str, Any]] = []
    for sample in all_local_pack_samples():
        kind = str(sample.get("expected_hf_class") or "")
        if doc_class and kind != doc_class:
            continue
        cases.append(
            {
                "id": f"pack:{kind}/{sample.get('filename')}",
                "filename": sample.get("filename"),
                "text": sample.get("text") or "",
                "expected_doc_class": kind,
                "expected_subclass": sample.get("expected_subclass") or "",
                "expected_fields": dict(sample.get("expected_fields") or {}),
                "source": "local_pack",
                "mock_only": True,
            }
        )
    return cases


def load_manifest_cases(*, mock: bool, doc_class: str | None = None) -> list[dict[str, Any]]:
    import csv

    from scripts.prepare_samples import is_real_sample

    if not MANIFEST.exists():
        return []
    cases: list[dict[str, Any]] = []
    with MANIFEST.open() as fh:
        for row in csv.DictReader(fh):
            kind = row.get("expected_doc_class") or ""
            if doc_class and kind != doc_class:
                continue
            real = is_real_sample(row)
            if not mock and not real:
                continue
            fields = {}
            raw = (row.get("expected_fields") or "").strip()
            if raw:
                try:
                    fields = json.loads(raw)
                except json.JSONDecodeError:
                    fields = {}
            cases.append(
                {
                    "id": f"manifest:{row.get('id')}",
                    "filename": row.get("filename"),
                    "expected_doc_class": kind,
                    "expected_stage": row.get("expected_stage"),
                    "expected_fields": fields,
                    "source": "manifest",
                    "mock_only": not real,
                    "row": row,
                }
            )
    return cases


def cases_for_agent(agent_name: str, *, mock: bool = True, n: int | None = None) -> list[dict[str, Any]]:
    """Labeled cases for one agent. Specialists get pack+fixture+manifest."""
    if agent_name not in LLM_AGENTS:
        raise ValueError(f"unknown agent {agent_name!r}; choose from {LLM_AGENTS}")
    cases: list[dict[str, Any]] = []
    if agent_name == "sorter" or agent_name == "sorter_reviewer":
        cases.extend(load_fixture_cases())
        cases.extend(load_local_pack_cases())
        cases.extend(load_manifest_cases(mock=mock))
    elif agent_name in SPECIALIST_FOR_CLASS.values():
        classes = [c for c, a in SPECIALIST_FOR_CLASS.items() if a == agent_name]
        for kind in classes:
            cases.extend(load_local_pack_cases(kind))
            cases.extend(load_fixture_cases(kind))
            cases.extend(load_manifest_cases(mock=mock, doc_class=kind))
    elif agent_name in ("judge", "arbiter", "boss", "reporter"):
        cases.extend(load_local_pack_cases())
        cases.extend(load_fixture_cases())
    elif agent_name in ("pdf_transcriber", "image_extractor"):
        cases.extend(load_fixture_cases())
    if n is not None:
        cases = cases[: max(0, int(n))]
    return cases


def score_classification(predicted_class: str | None, expected_class: str | None) -> dict[str, Any]:
    from observability.classification_scoring import classes_match

    expected = expected_class or ""
    predicted = predicted_class or ""
    correct = int(classes_match(expected, predicted)) if expected else None
    return {
        "predicted_doc_class": predicted,
        "expected_doc_class": expected,
        "class_correct": correct,
    }


def score_extraction_case(
    doc_class: str,
    predicted: dict,
    expected: dict,
) -> dict[str, Any]:
    if not expected:
        return {"overall_score": None, "n_expected_fields": 0}
    from observability.field_scoring import get_field_types
    from observability.suite_scoring import score_with_suite

    result, extras = score_with_suite(
        doc_class,
        predicted,
        expected,
        field_types=get_field_types(doc_class),
    )
    out: dict[str, Any] = {
        "overall_score": None if result.overall_score is None else round(float(result.overall_score), 4),
        "needs_judge_review": bool(result.needs_judge_review),
        "n_expected_fields": len(expected),
    }
    for key, value in extras.items():
        out[key] = round(float(value), 4)
    return out


def _invoke_sorter(text: str) -> dict[str, Any]:
    from agents.sorter import SorterAgent

    agent = SorterAgent()
    doc_type, subtype, confidence, reasoning = agent.classify(text)
    return {
        "doc_type": doc_type,
        "contract_subtype": subtype,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def _invoke_reviewer(text: str) -> dict[str, Any]:
    from agents.sorter_reviewer import SorterReviewerAgent

    return SorterReviewerAgent().review(text)


def _invoke_specialist(agent_name: str, text: str) -> dict[str, Any]:
    mapping = {
        "contracts_specialist": ("agents.contracts_specialist", "ContractsSpecialist"),
        "corporate_records_specialist": (
            "agents.corporate_records_specialist",
            "CorporateRecordsSpecialist",
        ),
        "correspondence_specialist": (
            "agents.correspondence_specialist",
            "CorrespondenceSpecialist",
        ),
        "compliance_specialist": ("agents.compliance_specialist", "ComplianceSpecialist"),
        "insurance_claims_specialist": (
            "agents.insurance_claims_specialist",
            "InsuranceClaimsSpecialist",
        ),
    }
    mod_name, cls_name = mapping[agent_name]
    import importlib

    cls = getattr(importlib.import_module(mod_name), cls_name)
    return cls().extract(text)


def invoke_agent(agent_name: str, case: dict[str, Any]) -> dict[str, Any]:
    """Run one agent on one case. Caller is responsible for mock/real LLM."""
    text = case.get("text") or ""
    doc_class = case.get("expected_doc_class") or ""
    if agent_name == "sorter":
        return _invoke_sorter(text)
    if agent_name == "sorter_reviewer":
        return _invoke_reviewer(text)
    if agent_name in SPECIALIST_FOR_CLASS.values():
        return _invoke_specialist(agent_name, text)
    if agent_name == "judge":
        from agents.judge import CompletenessJudge

        extracted = case.get("predicted_fields") or case.get("expected_fields") or {}
        return CompletenessJudge().judge_completeness(doc_class, extracted, text)
    if agent_name == "arbiter":
        from agents.arbiter import ArbiterAgent

        extracted = case.get("predicted_fields") or case.get("expected_fields") or {}
        return ArbiterAgent().arbitrate(
            doc_type=doc_class,
            extracted=extracted,
            judge_verdict=case.get("judge_verdict") or "incomplete",
            judge_findings=case.get("judge_findings") or ["isolation eval"],
            judge_score=case.get("judge_score"),
        )
    if agent_name == "boss":
        from agents.boss import BossAgent

        return BossAgent().adjudicate(
            {
                "doc_id": case.get("id"),
                "doc_type": doc_class,
                "extracted_data": case.get("expected_fields") or {},
                "escalation_reason": "isolation eval",
            }
        )
    if agent_name == "reporter":
        from agents.reporter import compile_matter_record
        from llm.client import get_llm

        client, model = get_llm("reporter")
        return compile_matter_record(
            {
                "doc_type": doc_class,
                "extracted_data": case.get("expected_fields") or {},
                "classification_confidence": 0.9,
                "extraction_confidence": 0.9,
            },
            client,
            model,
        )
    if agent_name == "pdf_transcriber":
        path = case.get("path")
        if path:
            from agents.pdf_transcriber import PDFTranscriber

            return PDFTranscriber().transcribe(Path(path))
        return {"markdown": text, "confidence": 1.0, "method": "passthrough"}
    if agent_name == "image_extractor":
        path = case.get("path")
        if path:
            from agents.image_extractor import ImageExtractor

            return ImageExtractor().extract(Path(path))
        return {"text": text, "confidence": 1.0, "method": "passthrough"}
    raise ValueError(f"no invoke path for {agent_name}")


def score_case(agent_name: str, case: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    expected_class = case.get("expected_doc_class")
    scored: dict[str, Any] = {"id": case.get("id"), "agent": agent_name}
    if agent_name in ("sorter", "sorter_reviewer"):
        predicted = prediction.get("doc_type")
        scored.update(score_classification(predicted, expected_class))
        return scored
    if agent_name in SPECIALIST_FOR_CLASS.values():
        scored.update(
            score_extraction_case(
                expected_class or "",
                prediction,
                case.get("expected_fields") or {},
            )
        )
        return scored
    if agent_name == "reporter":
        blob = json.dumps(prediction, default=str)
        expected_keys = list((case.get("expected_fields") or {}).keys())
        present = sum(1 for k in expected_keys if k in blob)
        scored["key_presence"] = (
            None if not expected_keys else round(present / len(expected_keys), 4)
        )
        return scored
    if agent_name in ("pdf_transcriber", "image_extractor"):
        predicted_text = prediction.get("markdown") or prediction.get("text") or ""
        scored["token_overlap"] = round(token_overlap(predicted_text, case.get("text") or ""), 4)
        return scored
    if agent_name == "boss":
        scored["decision"] = prediction.get("decision")
        scored["decision_valid"] = int(prediction.get("decision") in ("approved", "review"))
        return scored
    if agent_name == "arbiter":
        scored["decision"] = prediction.get("decision")
        scored["decision_valid"] = int(
            prediction.get("decision")
            in ("accept_with_caveats", "retry_extraction", "human_review")
        )
        return scored
    if agent_name == "judge":
        scored["completeness"] = prediction.get("completeness")
        scored["completeness_label"] = prediction.get("completeness_label")
        return scored
    return scored


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    class_correct = [r["class_correct"] for r in rows if isinstance(r.get("class_correct"), int)]
    overall = [r["overall_score"] for r in rows if isinstance(r.get("overall_score"), (int, float))]
    overlap = [r["token_overlap"] for r in rows if isinstance(r.get("token_overlap"), (int, float))]
    valid = [r["decision_valid"] for r in rows if isinstance(r.get("decision_valid"), int)]
    keys = [r["key_presence"] for r in rows if isinstance(r.get("key_presence"), (int, float))]
    return {
        "n": len(rows),
        "class_accuracy": _mean([float(v) for v in class_correct]),
        "extraction_overall_mean": _mean(overall),
        "token_overlap_mean": _mean(overlap),
        "decision_valid_rate": _mean([float(v) for v in valid]),
        "reporter_key_presence_mean": _mean(keys),
    }


def evaluate_agent(
    agent_name: str,
    *,
    mock: bool = True,
    n: int | None = None,
    invoke: bool = True,
    invoker: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate one agent in isolation. Returns a report dict."""
    cases = cases_for_agent(agent_name, mock=mock, n=n)
    rows: list[dict[str, Any]] = []
    errors = 0
    call = invoker or invoke_agent
    for case in cases:
        prediction: dict[str, Any]
        if invoke:
            try:
                prediction = call(agent_name, case)
            except Exception as exc:
                errors += 1
                rows.append(
                    {
                        "id": case.get("id"),
                        "agent": agent_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
        else:
            # Scorer self-check: gold in, gold out.
            prediction = dict(case.get("expected_fields") or {})
            if agent_name in ("sorter", "sorter_reviewer"):
                prediction = {"doc_type": case.get("expected_doc_class")}
        rows.append(score_case(agent_name, case, prediction if isinstance(prediction, dict) else {}))
    return {
        "agent": agent_name,
        "mock": mock,
        "metrics": summarize(rows),
        "errors": errors,
        "rows": rows,
    }
