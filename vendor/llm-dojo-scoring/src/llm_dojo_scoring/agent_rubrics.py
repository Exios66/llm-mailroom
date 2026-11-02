"""Small dependency-free isolation-eval rubrics; no model invocation or I/O."""
from __future__ import annotations


def token_set_f1(predicted: str, expected: str) -> float:
    """Lowercase whitespace token SET F1, blank/blank=1; punctuation retained.

    This is the historical agent-eval rubric, not SQuAD multiset token F1.
    """
    pred = {t for t in (predicted or "").lower().split() if t}
    exp = {t for t in (expected or "").lower().split() if t}
    if not pred and not exp:
        return 1.0
    if not pred or not exp:
        return 0.0
    precision, recall = len(pred & exp) / len(pred), len(pred & exp) / len(exp)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def key_presence(blob: str, expected_keys: list[str]) -> float | None:
    """Four-decimal case-sensitive substring presence; no expected keys => None.

    The producer owns serialization. A key appearing in a VALUE still counts,
    preserving the old rubric; this does not claim JSON schema completeness.
    """
    return (round(sum(k in blob for k in expected_keys) / len(expected_keys), 4)
            if expected_keys else None)


def decision_valid(decision, allowed: tuple[str, ...]) -> int:
    """Exact membership rubric; producer supplies its role's allowed decisions."""
    return int(decision in allowed)


def _denial_reasons_present(data: dict) -> bool:
    """Historical local-pack emptiness, including stringified sequence members.

    None inside a nonempty reason list counts as the literal string 'None'.
    This differs from recursive field emptiness and is deliberately preserved.
    """
    raw = data.get("denial_reasons")
    if raw is None:
        return False
    if isinstance(raw, (list, tuple, set)):
        return any(str(item).strip() for item in raw)
    return bool(str(raw).strip())


def insurance_gt_is_homogeneous(expected: dict | None) -> bool:
    """One CMS-shaped GT row: approved alias and no denial reasons."""
    return insurance_expected_set_is_homogeneous([expected])


def insurance_expected_set_is_homogeneous(expected_list: list[dict] | None) -> bool:
    """Ignore unlabeled rows; at least one label, all approved with empty denials."""
    labeled = 0
    for item in expected_list or []:
        data = item or {}
        det = str(data.get("coverage_determination") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not det:
            continue
        labeled += 1
        if det not in {"approved", "approve", "covered", "yes"} or _denial_reasons_present(data):
            return False
    return labeled > 0


def determination_consistency_is_quality(expected: dict | None) -> bool:
    """Retain legacy quality-gate metadata, including True for unlabeled GT."""
    return not insurance_gt_is_homogeneous(expected)
