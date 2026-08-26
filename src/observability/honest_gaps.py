"""Surface llm-dojo-scoring 0.9.0 honesty gaps without inventing metrics.

The dedicated specialist suites already carry ``honest_gap``, ``in_corpus``,
and ``retired``. Mailroom must pin those fields on traces and HF reports so
we never pretend a class has a CUAD-class extraction benchmark (or a
corpus-backed accuracy) that the registry does not.

Do **not** add SCORE_CONFIGS names here. Determination-consistency is not in
the installed dojo metric registry; the local insurance invariant below is a
field-consistency check, not a registered score.
"""

from __future__ import annotations

from typing import Any

# Hub / taxonomy extract classes whose v0.9.0 suites declare an honest gap.
# HF_CLASSES omits compliance (zero rows) and the retired court/DD types.
GAP_DOC_TYPES: tuple[str, ...] = (
    "insurance_claim",
    "compliance_filing",
    "corporate_record",
    "court_opinion",
    "due_diligence",
)

_DETERMINATIONS = frozenset({"approved", "denied", "partial", "pending"})


def suite_honesty(doc_class: str | None) -> dict[str, Any]:
    """Read-only honesty payload from ``get_suite(doc_class)``.

    Returns ``{}`` when the class has no suite. Never invents a gap string.
    """
    kind = str(doc_class or "").strip()
    if not kind:
        return {}
    try:
        from llm_dojo_scoring import get_suite

        suite = get_suite(kind)
    except Exception:
        return {}
    if suite is None:
        return {}
    gap = getattr(suite, "honest_gap", None)
    return {
        "suite_name": getattr(suite, "name", None),
        "doc_type": getattr(suite, "doc_type", kind),
        "in_corpus": bool(getattr(suite, "in_corpus", False)),
        "retired": bool(getattr(suite, "retired", False)),
        "honest_gap": gap or None,
        "subclasses": list(getattr(suite, "subclasses", None) or ()),
    }


def honesty_trace_metadata(
    doc_class: str | None,
    extracted: dict | None = None,
) -> dict[str, Any]:
    """Slim JSON-serializable honesty block for Langfuse metadata (not tags)."""
    payload = suite_honesty(doc_class)
    if not payload:
        return {}
    out: dict[str, Any] = {
        "suite_name": payload.get("suite_name"),
        "in_corpus": payload.get("in_corpus"),
        "retired": payload.get("retired"),
        "honest_gap": payload.get("honest_gap"),
    }
    kind = str(payload.get("doc_type") or doc_class or "")
    if kind == "insurance_claim":
        consistent = insurance_determination_consistent(extracted)
        if consistent is not None:
            out["determination_consistent"] = consistent
            issues = insurance_determination_issues(extracted)
            if issues:
                out["determination_issues"] = issues
    return out


def _denial_reasons(extracted: dict | None) -> list[str]:
    raw = (extracted or {}).get("denial_reasons")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    return [text] if text else []


def insurance_determination_issues(extracted: dict | None) -> list[str]:
    """Local coverage_determination ↔ denial_reasons invariant.

    Not a dojo metric. CMS DE-SynPUF ground truth is all
    ``coverage_determination=approved`` with empty ``denial_reasons``, so the
    Hub rows never exercise the denied path. This check flags internally
    contradictory extracts (denied with no reasons, approved with reasons)
    without claiming amount-exactness or a registered determination-consistency
    score.
    """
    data = extracted or {}
    det = str(data.get("coverage_determination") or "").strip().lower()
    if not det:
        return []
    if det not in _DETERMINATIONS:
        return [f"unknown_determination:{det}"]
    reasons = _denial_reasons(data)
    issues: list[str] = []
    if det == "denied" and not reasons:
        issues.append("denied_without_reasons")
    if det == "approved" and reasons:
        issues.append("approved_with_denial_reasons")
    if det == "pending" and reasons:
        issues.append("pending_with_denial_reasons")
    return issues


def insurance_determination_consistent(extracted: dict | None) -> bool | None:
    """True/False when a determination is present; None when there is nothing to check."""
    det = str((extracted or {}).get("coverage_determination") or "").strip()
    if not det:
        return None
    return not insurance_determination_issues(extracted)
