"""Surface llm-dojo-scoring honesty gaps without inventing metrics.

The dedicated specialist suites carry ``honest_gap``, ``in_corpus``,
and ``retired``. Mailroom pins those fields on traces and HF reports.

v0.10.0 registered ``determination_consistency`` / ``amount_exactness``.
The remaining insurance Hub gap is CMS GT homogeneity (all-approved / empty
denials), not a missing scorer: that extra is gated as a quality KPI on
homogeneous GT and exercised on the local contrast pack
(``observability.local_eval_packs``). Local ``insurance_determination_*``
helpers stay as a guardrail/metadata invariant.
"""

from __future__ import annotations

from typing import Any

# Hub / taxonomy extract classes whose suites still declare an honest gap.
# HF_CLASSES omits compliance (zero rows) and the retired court/DD types.
GAP_DOC_TYPES: tuple[str, ...] = (
    "insurance_claim",
    "compliance_filing",
    "corporate_record",
    "court_opinion",
    "due_diligence",
)

_DETERMINATIONS = frozenset({"approved", "denied", "partial", "pending"})
_APPROVED = frozenset({"approved", "approve", "covered", "yes"})


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

    Not a substitute for the registered ``determination_consistency`` score.
    CMS DE-SynPUF ground truth is all ``coverage_determination=approved``
    with empty ``denial_reasons``, so Hub rows make that score degenerate
    (always 1.0 on GT-shaped predictions). This helper flags internally
    contradictory extracts for the extraction guard / trace metadata.
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


def _norm_determination(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def insurance_gt_is_homogeneous(expected: dict | None) -> bool:
    """True when one GT row is the CMS shape: approved (or alias) + empty denials."""
    data = expected or {}
    det = _norm_determination(data.get("coverage_determination"))
    if not det:
        return False
    return det in _APPROVED and not _denial_reasons(data)


def insurance_expected_set_is_homogeneous(expected_list: list[dict] | None) -> bool:
    """True when every labeled determination in the set is approved + empty reasons.

    Empty / unlabeled rows are ignored. A mixed approved/denied/partial set is
    not homogeneous. Used to gate Hub ``determination_consistency`` so a 1.0 on
    all-approved CMS GT is not reported as a quality KPI.
    """
    labeled = 0
    for item in expected_list or []:
        det = _norm_determination((item or {}).get("coverage_determination"))
        if not det:
            continue
        labeled += 1
        if det not in _APPROVED or _denial_reasons(item):
            return False
    return labeled > 0


def determination_consistency_is_quality(expected: dict | None) -> bool:
    """Hub CMS-shaped GT makes the registered extra tautological — do not headline it."""
    return not insurance_gt_is_homogeneous(expected)
