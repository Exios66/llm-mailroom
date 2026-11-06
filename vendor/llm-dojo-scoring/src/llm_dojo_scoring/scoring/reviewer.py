"""Reviewer-lane scoring for LLM and decision-model reviewers.

The reviewer lane is richer than a label classifier: it decides whether a
document continues unattended, parks for a person, or is unavailable, and it
may correct a subclass. This module owns the scorecard for that lane:

* class and subclass accuracy against verified targets;
* auto-accept risk (wrong accepts, exact upper bound, required sample size);
* confidence calibration (ECE, Brier, reliability bins);
* routing, cause counts, and accept-rule violations;
* the model's verdict confusion and cost per accepted document;
* per-predicted-class operating points and cross-fit cutoffs.

It is deterministic and network-free. Consumers map their run store to
:class:`ReviewRecord`; they keep run loading, target selection, question sets,
and cutoff application.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..analysis.bootstrap import wilson_ci
from ..analysis.calibration import (
    brier_score,
    clopper_pearson_upper,
    cross_fit_cutoffs,
    cutoff_at_target,
    expected_calibration_error,
    reliability_bins,
    required_samples,
)
from ..metrics.scorecard_honesty import SCORER_VERSION, metric_id_for
from ..schemas.equivalences import normalize_subtype
from ..schemas.sorter_guide import UNANNOTATED_SUBCLASSES
from .classification import confusion_matrix, per_class_stats

__all__ = [
    "HUMAN_VERDICTS",
    "CONTINUE_VERDICTS",
    "DECISION_VERDICTS",
    "ReviewRecord",
    "review_record_from_run_item",
    "score_review_lane",
]

#: Verdicts that always send a document to a person.
HUMAN_VERDICTS: tuple[str, ...] = (
    "route_to_human_review",
    "unreadable",
    "out_of_scope",
    "multiple_documents",
)

#: Verdicts that continue the document to extraction.
CONTINUE_VERDICTS: tuple[str, ...] = (
    "continue_sorter_correct",
    "continue_with_subclass_correction",
    "reclassify",
)

DECISION_VERDICTS: tuple[str, ...] = CONTINUE_VERDICTS + HUMAN_VERDICTS


def _label(value: Any) -> str | None:
    """Normalize a label to a stripped string, or ``None`` when absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _confidence(value: Any) -> float | None:
    """Return a finite confidence in [0, 1], excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if 0.0 <= number <= 1.0 else None


def _same_label(expected: Any, predicted: Any) -> bool:
    """Case-insensitive equality over normalized labels."""
    left, right = _label(expected), _label(predicted)
    return left is not None and right is not None and left.lower() == right.lower()


def _same_subclass(doc_type: str | None, predicted: Any, expected: Any) -> bool | None:
    """Compare subclasses; contract labels use the CUAD equivalence table."""
    left, right = _label(predicted), _label(expected)
    if left is None or right is None:
        return None
    if doc_type == "contract":
        return normalize_subtype(left) == normalize_subtype(right)
    return left.lower() == right.lower()


def _subclass_scorable(doc_type: str | None, subclass: Any) -> bool:
    """Whether a subclass is a real target for this class.

    Merger ``other`` marks an unannotated consideration type; it is not a
    label a reviewer can predict.
    """
    token = _label(subclass)
    if token is None:
        return False
    return token not in UNANNOTATED_SUBCLASSES.get(doc_type or "", ())


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a cause list, string, or None into a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value),)


@dataclass(frozen=True)
class ReviewRecord:
    """One document's reviewer-lane evidence and target.

    ``route`` is the pipeline destination (``extract`` / ``human_review`` /
    ``failed``). ``reviewer_verdict`` is the model's own question answer, not
    the pipeline's ``decision_accepts`` / ``decision_escalates`` route label.
    """

    doc_id: str
    expected_doc_type: str | None = None
    expected_doc_subclass: str | None = None
    expected_subclass_scorable: bool | None = None

    sorter_doc_type: str | None = None
    sorter_doc_subclass: str | None = None
    sorter_confidence: float | None = None
    sorter_below_low_band: bool = False

    reviewer_doc_type: str | None = None
    reviewer_doc_subclass: str | None = None
    reviewer_confidence: float | None = None
    reviewer_model: str | None = None
    reviewer_verdict: str | None = None
    route: str | None = None
    route_verdict: str | None = None
    causes: tuple[str, ...] = ()
    eligible: bool = True
    cutoff: float | None = None
    has_text: bool = True
    error: str | None = None

    human_decision: str | None = None
    human_doc_type: str | None = None
    human_doc_subclass: str | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    question_set: str | None = None
    dataset_revision: str | None = None
    split: str | None = None
    draw_seed: str | None = None
    model_id: str | None = None
    serving_kind: str | None = None

    def __post_init__(self) -> None:
        """Fill the subclass-scorable default from the class catalog."""
        if self.expected_subclass_scorable is None:
            object.__setattr__(
                self,
                "expected_subclass_scorable",
                _subclass_scorable(self.expected_doc_type, self.expected_doc_subclass),
            )
        route = _label(self.route)
        if route in {"accept", "accepted", "auto_accept"}:
            route = "extract"
        elif route in {"review", "human", "parked"}:
            route = "human_review"
        object.__setattr__(self, "route", route)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ReviewRecord":
        """Build a record from a run-store row, pipeline manifest, or API payload."""
        usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
        provenance = row.get("provenance") if isinstance(row.get("provenance"), Mapping) else {}
        expected_subclass = (
            row.get("expected_doc_subclass")
            if row.get("expected_doc_subclass") is not None
            else row.get("expected_subclass")
        )
        scorable = row.get("expected_subclass_scorable")
        return cls(
            doc_id=str(row.get("doc_id") or row.get("id") or row.get("item_id") or ""),
            expected_doc_type=_label(
                row.get("expected_doc_type")
                or row.get("expected_doc_class")
                or row.get("expected_label")
                or row.get("truth")
            ),
            expected_doc_subclass=_label(expected_subclass),
            expected_subclass_scorable=scorable if isinstance(scorable, bool) else None,
            sorter_doc_type=_label(
                row.get("sorter_doc_type") or row.get("sorter_label") or row.get("sorter")
            ),
            sorter_doc_subclass=_label(
                row.get("sorter_doc_subclass") or row.get("sorter_subclass")
            ),
            sorter_confidence=_confidence(
                row.get("sorter_confidence", row.get("sorter_conf"))
            ),
            sorter_below_low_band=bool(row.get("sorter_below_low_band", False)),
            reviewer_doc_type=_label(
                row.get("reviewer_doc_type")
                or row.get("reviewer_label")
                or row.get("predicted_doc_type")
                or row.get("pred_doc_type")
                or row.get("doc_type")
            ),
            reviewer_doc_subclass=_label(
                row.get("reviewer_doc_subclass")
                or row.get("reviewer_subclass")
                or row.get("predicted_doc_subclass")
                or row.get("pred_doc_subclass")
                or row.get("doc_subclass")
            ),
            reviewer_confidence=_confidence(
                row.get("reviewer_confidence", row.get("confidence", row.get("pred_confidence")))
            ),
            reviewer_model=_label(
                row.get("reviewer_model") or row.get("model") or row.get("decision_model")
            ),
            reviewer_verdict=_label(row.get("reviewer_verdict") or row.get("verdict")),
            route=_label(row.get("route") or row.get("review_route") or row.get("destination")),
            route_verdict=_label(row.get("route_verdict") or row.get("review_verdict")),
            causes=_as_tuple(row.get("causes") or row.get("review_causes") or row.get("reviewer_causes")),
            eligible=bool(row.get("eligible", True)),
            cutoff=_confidence(row.get("cutoff")),
            has_text=bool(row.get("has_text", True)),
            error=_label(row.get("error")),
            human_decision=_label(row.get("human_decision")),
            human_doc_type=_label(row.get("human_doc_type")),
            human_doc_subclass=_label(row.get("human_doc_subclass")),
            prompt_tokens=row.get("prompt_tokens", usage.get("prompt_tokens")),
            completion_tokens=row.get("completion_tokens", usage.get("completion_tokens")),
            cost_usd=_cost(row.get("cost_usd"), row.get("cost")),
            question_set=_label(
                row.get("question_set")
                or row.get("questions")
                or row.get("prompt_id")
                or provenance.get("prompt_id")
            ),
            dataset_revision=_label(
                row.get("dataset_revision") or provenance.get("dataset_revision")
            ),
            split=_label(row.get("split") or provenance.get("split")),
            draw_seed=_label(row.get("draw_seed") or provenance.get("draw_seed")),
            model_id=_label(row.get("model_id") or provenance.get("model_id")),
            serving_kind=_label(row.get("serving_kind") or provenance.get("serving_kind")),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the record as a JSON-serializable mapping."""
        return {
            "doc_id": self.doc_id,
            "expected_doc_type": self.expected_doc_type,
            "expected_doc_subclass": self.expected_doc_subclass,
            "expected_subclass_scorable": self.expected_subclass_scorable,
            "sorter_doc_type": self.sorter_doc_type,
            "sorter_doc_subclass": self.sorter_doc_subclass,
            "sorter_confidence": self.sorter_confidence,
            "sorter_below_low_band": self.sorter_below_low_band,
            "reviewer_doc_type": self.reviewer_doc_type,
            "reviewer_doc_subclass": self.reviewer_doc_subclass,
            "reviewer_confidence": self.reviewer_confidence,
            "reviewer_model": self.reviewer_model,
            "reviewer_verdict": self.reviewer_verdict,
            "route": self.route,
            "route_verdict": self.route_verdict,
            "causes": list(self.causes),
            "eligible": self.eligible,
            "cutoff": self.cutoff,
            "has_text": self.has_text,
            "error": self.error,
            "human_decision": self.human_decision,
            "human_doc_type": self.human_doc_type,
            "human_doc_subclass": self.human_doc_subclass,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "question_set": self.question_set,
            "dataset_revision": self.dataset_revision,
            "split": self.split,
            "draw_seed": self.draw_seed,
            "model_id": self.model_id,
            "serving_kind": self.serving_kind,
        }


def _first_present(*values: Any) -> Any:
    """Return the first value that is not ``None``, keeping measured zeros."""
    for value in values:
        if value is not None:
            return value
    return None


def _cost(*candidates: Any) -> float | None:
    """First finite numeric cost; NaN / inf candidates are skipped, never totalled."""
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                return number
    return None


def review_record_from_run_item(
    item: Mapping[str, Any],
    *,
    truth: Mapping[str, Any] | None = None,
    handoff: Mapping[str, Any] | None = None,
    cutoffs: Mapping[str, float] | None = None,
    lock: Mapping[str, Any] | None = None,
) -> ReviewRecord:
    """Map a sandbox-style ``items.jsonl`` row to a :class:`ReviewRecord`.

    The run store keeps the reviewer answer under ``pred`` and the sorter
    handoff separately. ``truth`` is the dataset row, ``cutoffs`` the frozen
    per-class cutoffs, and ``lock`` the run lock (for model, question set,
    dataset revision and split). No value is invented: a missing answer or
    truth stays ``None``.
    """
    pred = item.get("pred") if isinstance(item.get("pred"), Mapping) else {}
    truth = truth or {}
    handoff = handoff or {}
    lock = lock or {}
    reviewer_cfg = lock.get("reviewer") if isinstance(lock.get("reviewer"), Mapping) else {}
    dataset = lock.get("dataset") if isinstance(lock.get("dataset"), Mapping) else {}
    engine = lock.get("engine") if isinstance(lock.get("engine"), Mapping) else {}
    usage = pred.get("usage") if isinstance(pred.get("usage"), Mapping) else {}
    triggers = handoff.get("review_trigger")
    trigger_list = triggers if isinstance(triggers, (list, tuple)) else []
    doc_type = _label(pred.get("doc_type"))
    route_to_human = pred.get("route_to_human")
    frozen_cutoffs = cutoffs or reviewer_cfg.get("classes") or {}
    return ReviewRecord.from_mapping(
        {
            "doc_id": item.get("item_id") or item.get("doc_id") or item.get("id"),
            "expected_doc_type": truth.get("expected_doc_class"),
            "expected_doc_subclass": truth.get("expected_subclass"),
            "sorter_doc_type": handoff.get("sorter_doc_type"),
            "sorter_doc_subclass": handoff.get("sorter_doc_subclass"),
            "sorter_confidence": handoff.get("sorter_confidence"),
            "sorter_below_low_band": "sorter_below_low_band" in trigger_list,
            "reviewer_doc_type": doc_type,
            "reviewer_doc_subclass": pred.get("doc_subclass") or pred.get("contract_subtype"),
            "reviewer_confidence": pred.get("confidence"),
            "reviewer_verdict": pred.get("verdict"),
            "route": "human_review" if route_to_human else None,
            "cutoff": frozen_cutoffs.get(doc_type) if doc_type else None,
            "has_text": bool(item.get("has_text", True)),
            "error": item.get("error"),
            "reviewer_model": reviewer_cfg.get("model") or pred.get("served_model"),
            "question_set": reviewer_cfg.get("questions") or lock.get("questions"),
            "prompt_tokens": _first_present(usage.get("input_tokens"), usage.get("prompt_tokens")),
            "completion_tokens": _first_present(
                usage.get("output_tokens"), usage.get("completion_tokens")
            ),
            "cost_usd": _first_present(usage.get("cost"), usage.get("cost_usd")),
            "dataset_revision": dataset.get("revision"),
            "split": dataset.get("split"),
            "draw_seed": dataset.get("sample_seed"),
            "serving_kind": engine.get("kind"),
        }
    )


def _route(record: ReviewRecord) -> str | None:
    """Resolve the pipeline destination, or ``None`` when the row has none."""
    if record.route in {"extract", "human_review", "failed"}:
        return record.route
    if record.route_verdict == "decision_accepts":
        return "extract"
    if record.route_verdict == "decision_escalates":
        return "human_review"
    if record.causes or record.error:
        return "human_review"
    if record.reviewer_doc_type is None:
        return "human_review"
    if not record.has_text:
        return "human_review"
    if record.cutoff is not None and record.reviewer_confidence is not None:
        if record.reviewer_confidence < record.cutoff:
            return "human_review"
    if record.sorter_doc_type and not record.sorter_below_low_band:
        inferred = (
            "extract" if _same_label(record.sorter_doc_type, record.reviewer_doc_type) else "human_review"
        )
    else:
        inferred = "extract" if record.sorter_below_low_band else None
    if inferred == "extract" and _accept_rule_violations(record):
        # No route was recorded, so the scorecard applies the accept rule
        # itself: a row with no cutoff, no confidence, or an ineligible label
        # cannot be inferred into the auto-accept population.
        return "human_review"
    return inferred


def _answered(record: ReviewRecord) -> bool:
    """Whether the reviewer produced a usable answer for quality scoring."""
    return bool(record.reviewer_doc_type) and not record.error and record.has_text


def _accepted(record: ReviewRecord) -> bool:
    """Whether the document continued unattended."""
    return _route(record) == "extract"


def _human_routed(record: ReviewRecord) -> bool:
    """Whether the document reached a person (or had no usable reviewer)."""
    return _route(record) in {"human_review", "failed"}


def _expected_verdict(record: ReviewRecord) -> str | None:
    """Derive the verdict the reviewer should have given from the handoff."""
    expected = _label(record.expected_doc_type)
    sorter = _label(record.sorter_doc_type)
    if expected is None or sorter is None:
        return None
    if not _same_label(sorter, expected):
        return "reclassify"
    if record.expected_subclass_scorable and record.sorter_doc_subclass is not None:
        same = _same_subclass(expected, record.sorter_doc_subclass, record.expected_doc_subclass)
        if same is False:
            return "continue_with_subclass_correction"
    return "continue_sorter_correct"


def _accept_rule_violations(record: ReviewRecord) -> list[str]:
    """Return every accept-rule failure for one row routed to extraction."""
    violations: list[str] = []
    if not record.has_text:
        violations.append("accept_without_text")
    if record.reviewer_doc_type is None:
        violations.append("accept_without_reviewer_label")
    elif not record.eligible:
        violations.append("accept_ineligible_reviewer_label")
    if record.causes:
        violations.append("accept_with_causes")
    if record.cutoff is None:
        violations.append("accept_without_cutoff")
    elif record.reviewer_confidence is None or record.reviewer_confidence < record.cutoff:
        violations.append("accept_below_cutoff")
    if record.sorter_doc_type and not record.sorter_below_low_band and not _same_label(
        record.sorter_doc_type, record.reviewer_doc_type
    ):
        violations.append("accept_disagreement")
    if record.reviewer_verdict in HUMAN_VERDICTS:
        violations.append("accept_human_verdict")
    return violations


def _unique(values: Iterable[Any]) -> str | None:
    """Return the single non-empty value, or ``None`` when absent or mixed."""
    present = sorted({str(value) for value in values if value not in (None, "")})
    return present[0] if len(present) == 1 else None


def _unique_list(values: Iterable[Any]) -> list[str]:
    """Return every distinct non-empty value, sorted."""
    return sorted({str(value) for value in values if value not in (None, "")})


def _class_metrics(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """Class accuracy, macro accuracy, per-class stats, and confusion."""
    scored = [
        (record.expected_doc_type, record.reviewer_doc_type)
        for record in records
        if record.expected_doc_type and _answered(record)
    ]
    n_expected = sum(1 for record in records if record.expected_doc_type)
    n_scored = len(scored)
    correct = sum(1 for exp, pred in scored if _same_label(exp, pred))
    expected_labels = [str(exp) for exp, _ in scored]
    predicted_labels = [str(pred) for _, pred in scored]
    per_class = per_class_stats(expected_labels, predicted_labels) if scored else {}
    matrix, labels = confusion_matrix(expected_labels, predicted_labels) if scored else ([], [])
    macro = (
        round(sum(stats["accuracy"] for stats in per_class.values()) / len(per_class), 6)
        if per_class
        else None
    )
    return {
        "class_accuracy": round(correct / n_scored, 6) if n_scored else None,
        "class_accuracy_ci": wilson_ci(correct / n_scored, n_scored) if n_scored else None,
        "class_macro_accuracy": macro,
        "n_expected": n_expected,
        "n_scored": n_scored,
        "n_unanswered": max(0, n_expected - n_scored),
        "coverage": round(n_scored / n_expected, 6) if n_expected else None,
        "correct": correct,
        "per_class": per_class,
        "confusion": {"matrix": matrix, "labels": labels},
    }


def _subclass_metrics(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """Subclass accuracy over rows whose class the reviewer got right."""
    by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "scored": 0})
    accepted_wrong = 0
    accepted_scored = 0
    for record in records:
        doc_type = _label(record.expected_doc_type)
        if doc_type is None or not _answered(record) or not _same_label(
            record.reviewer_doc_type, doc_type
        ):
            continue
        if not record.expected_subclass_scorable:
            continue
        if _label(record.reviewer_doc_subclass) is None:
            # A scorable target with no predicted subclass is a miss, as in
            # the pipeline subclass scorer; skipping it would inflate accuracy.
            same: bool | None = False
        else:
            same = _same_subclass(doc_type, record.reviewer_doc_subclass, record.expected_doc_subclass)
        if same is None:
            continue
        by_class[doc_type]["scored"] += 1
        by_class[doc_type]["correct"] += int(same)
        if _accepted(record):
            accepted_scored += 1
            if not same:
                accepted_wrong += 1
    correct = sum(cell["correct"] for cell in by_class.values())
    scored = sum(cell["scored"] for cell in by_class.values())
    return {
        "subclass_correct": correct,
        "subclass_scored": scored,
        "subclass_accuracy": round(correct / scored, 6) if scored else None,
        "by_class": {
            name: {
                "correct": cell["correct"],
                "scored": cell["scored"],
                "accuracy": round(cell["correct"] / cell["scored"], 6) if cell["scored"] else None,
            }
            for name, cell in sorted(by_class.items())
        },
        "accepted_subclass_scored": accepted_scored,
        "accepted_subclass_wrong": accepted_wrong,
        "accepted_subclass_wrong_rate": (
            round(accepted_wrong / accepted_scored, 6) if accepted_scored else None
        ),
    }


def _verdict_metrics(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """Verdict accuracy and confusion against the derivable expected verdict."""
    pairs: list[tuple[str, str]] = []
    for record in records:
        expected = _expected_verdict(record)
        predicted = _label(record.reviewer_verdict)
        if expected is not None and predicted is not None:
            pairs.append((expected, predicted))
    labels = sorted({label for pair in pairs for label in pair})
    matrix = [[0] * len(labels) for _ in labels]
    index = {label: position for position, label in enumerate(labels)}
    by_expected: dict[str, dict[str, int]] = defaultdict(dict)
    for expected, predicted in pairs:
        matrix[index[expected]][index[predicted]] += 1
        by_expected[expected][predicted] = by_expected[expected].get(predicted, 0) + 1
    correct = sum(1 for expected, predicted in pairs if expected == predicted)
    return {
        "verdict_accuracy": round(correct / len(pairs), 6) if pairs else None,
        "n": len(pairs),
        "confusion": {"matrix": matrix, "labels": labels},
        "verdicts_by_expected": {
            expected: dict(sorted(predicted.items()))
            for expected, predicted in sorted(by_expected.items())
        },
        "human_verdicts": dict(
            Counter(
                predicted
                for _, predicted in pairs
                if predicted in HUMAN_VERDICTS
            ).most_common()
        ),
    }


def _calibration_metrics(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """ECE, Brier, reliability bins, and mean confidence split by correctness."""
    pairs = [
        (record.reviewer_confidence, _same_label(record.expected_doc_type, record.reviewer_doc_type))
        for record in records
        if record.expected_doc_type
        and _answered(record)
        and record.reviewer_confidence is not None
    ]
    if not pairs:
        return {
            "n": 0,
            "ece": None,
            "brier": None,
            "bins": [],
            "mean_confidence": None,
            "mean_confidence_correct": None,
            "mean_confidence_wrong": None,
        }
    correct_conf = [confidence for confidence, hit in pairs if hit]
    wrong_conf = [confidence for confidence, hit in pairs if not hit]
    return {
        "n": len(pairs),
        "ece": expected_calibration_error(pairs),
        "brier": brier_score(pairs),
        "bins": reliability_bins(pairs),
        "mean_confidence": round(sum(confidence for confidence, _ in pairs) / len(pairs), 6),
        "mean_confidence_correct": (
            round(sum(correct_conf) / len(correct_conf), 6) if correct_conf else None
        ),
        "mean_confidence_wrong": (
            round(sum(wrong_conf) / len(wrong_conf), 6) if wrong_conf else None
        ),
    }


def _acceptance_metrics(
    records: Sequence[ReviewRecord], *, risk: float, confidence: float
) -> dict[str, Any]:
    """Auto-accept risk: wrong accepts, exact bound, and required sample size."""
    accepted = [record for record in records if _accepted(record)]
    labelled = [record for record in accepted if record.expected_doc_type]
    with_truth = [record for record in labelled if _answered(record)]
    # A labelled row that continued unattended with no usable reviewer answer
    # (reviewer error, no label or no text) cannot be graded, but it is not
    # missing ground truth either: count it on its own so it stays visible.
    unanswered = len(labelled) - len(with_truth)
    wrong = sum(
        1 for record in with_truth if not _same_label(record.expected_doc_type, record.reviewer_doc_type)
    )
    n = len(with_truth)
    upper = clopper_pearson_upper(wrong, n, confidence=confidence) if n else None
    needed = required_samples(wrong, risk=risk, confidence=confidence)
    return {
        "n_accepted": len(accepted),
        "n_accepted_with_truth": n,
        "n_accepted_without_truth": len(accepted) - len(labelled),
        "n_accepted_unanswered": unanswered,
        "n_wrong_accepts": wrong,
        "wrong_accept_rate": round(wrong / n, 6) if n else None,
        "wrong_accept_upper_95": round(upper, 6) if upper is not None else None,
        "accepted_accuracy": round((n - wrong) / n, 6) if n else None,
        "required_accepts": needed,
        "sample_shortfall": max(0, needed - n) if needed is not None else None,
        "meets_risk_bar": bool(upper is not None and upper <= risk),
        "risk": risk,
        "confidence": confidence,
    }


def _routing_metrics(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """Human-review rate, cause counts, and accept-rule violations."""
    reached = [
        record
        for record in records
        if record.reviewer_doc_type is not None
        or record.route is not None
        or record.causes
        or record.error
    ]
    human = [record for record in reached if _human_routed(record)]
    by_class: dict[str, dict[str, Any]] = {}
    total_by_class: Counter[str] = Counter()
    routed_by_class: Counter[str] = Counter()
    for record in reached:
        name = _label(record.reviewer_doc_type or record.expected_doc_type) or "unknown"
        total_by_class[name] += 1
        if _human_routed(record):
            routed_by_class[name] += 1
    for name, total in sorted(total_by_class.items()):
        routed = routed_by_class[name]
        by_class[name] = {
            "routed": routed,
            "n": total,
            "rate": round(routed / total, 6) if total else None,
        }
    causes: Counter[str] = Counter()
    for record in reached:
        causes.update(record.causes)
    violations = []
    for record in records:
        if not _accepted(record):
            continue
        found = _accept_rule_violations(record)
        if found:
            violations.append({"doc_id": record.doc_id, "violations": found})
    return {
        "n_reached": len(reached),
        "n_human_review": len(human),
        "routed_to_human_review": len(human),
        "routed_to_human_review_rate": round(len(human) / len(reached), 6) if reached else None,
        "routed_to_human_review_by_class": by_class,
        "routed_to_human_review_basis": "reviewer",
        "cause_counts": dict(causes.most_common()),
        "cause_rates": {
            cause: round(count / len(reached), 6) if reached else None
            for cause, count in causes.most_common()
        },
        "accept_rule_violations": violations,
        "accept_rule_ok": not violations,
    }


def _cost_metrics(records: Sequence[ReviewRecord], *, n_accepted: int) -> dict[str, Any]:
    """Reviewer cost, refusing to price a partial subset as a whole run."""
    costs = [record.cost_usd for record in records if record.cost_usd is not None]
    priced = len(costs)
    unpriced = len(records) - priced
    complete = priced > 0 and unpriced == 0
    total = sum(costs) if costs else None
    return {
        "currency": "USD",
        "n_priced": priced,
        "n_unpriced": unpriced,
        "complete": complete,
        "priced_total_usd": round(total, 6) if total is not None else None,
        "total_usd": round(total, 6) if complete and total is not None else None,
        "per_document_usd": (
            round(total / len(records), 6) if complete and records and total is not None else None
        ),
        "per_accepted_usd": (
            round(total / n_accepted, 6) if complete and n_accepted and total is not None else None
        ),
    }


def _operating_points(
    records: Sequence[ReviewRecord],
    *,
    target_accuracy: float,
    risk: float,
    confidence: float,
    max_points: int = 20,
) -> dict[str, Any]:
    """Per-predicted-class cutoffs, cross-fit holdout, and a compact curve."""
    by_class: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for record in records:
        predicted = _label(record.reviewer_doc_type)
        if (
            predicted is None
            or predicted == "unknown"
            or record.reviewer_confidence is None
            or not _answered(record)
        ):
            continue
        if record.expected_doc_type is None:
            continue
        by_class[predicted].append(
            (record.reviewer_confidence, _same_label(predicted, record.expected_doc_type))
        )
    out: dict[str, Any] = {}
    for name, rows in sorted(by_class.items()):
        if not rows:
            continue
        unique = sorted({row[0] for row in rows})
        picked = unique
        if len(unique) > max_points:
            step = (len(unique) - 1) / (max_points - 1)
            picked = sorted({unique[round(index * step)] for index in range(max_points)})
        curve = []
        for cutoff in picked:
            accepted = [correct for value, correct in rows if value >= cutoff]
            if not accepted:
                continue
            wrong = sum(1 for correct in accepted if not correct)
            upper = clopper_pearson_upper(wrong, len(accepted), confidence=confidence)
            curve.append(
                {
                    "cutoff": cutoff,
                    "accepted": len(accepted),
                    "accuracy": round(sum(accepted) / len(accepted), 6),
                    "wrong_rate": round(wrong / len(accepted), 6),
                    "wrong_upper_95": round(upper, 6) if upper is not None else None,
                    "review_rate": round(1.0 - len(accepted) / len(rows), 6),
                }
            )
        # One descending pass: accumulate accepted / wrong counts and keep
        # the lowest inclusive cutoff whose exact bound clears ``risk``.
        risk_cutoff = None
        ordered = sorted(rows, key=lambda row: row[0], reverse=True)
        n_accepted = n_wrong = 0
        position = 0
        for cutoff in sorted(unique, reverse=True):
            while position < len(ordered) and ordered[position][0] >= cutoff:
                n_accepted += 1
                n_wrong += 0 if ordered[position][1] else 1
                position += 1
            if not n_accepted:
                continue
            upper = clopper_pearson_upper(n_wrong, n_accepted, confidence=confidence)
            if upper is not None and upper <= risk:
                risk_cutoff = cutoff
        out[name] = {
            "n": len(rows),
            "target_accuracy": target_accuracy,
            "cutoff_at_target": cutoff_at_target(rows, target_accuracy),
            # Exact, not rounded: an inclusive consumer must not admit a row
            # the bound check excluded.
            "risk_cutoff": risk_cutoff,
            **cross_fit_cutoffs(rows, target=target_accuracy, confidence=confidence),
            "curve": curve,
        }
    return out


def _provenance(records: Sequence[ReviewRecord]) -> dict[str, Any]:
    """One provenance block, or explicit mixed-identity lists."""
    question_sets = _unique_list(record.question_set for record in records)
    models = _unique_list(record.reviewer_model or record.model_id for record in records)
    return {
        "dataset_revision": _unique(record.dataset_revision for record in records),
        "split": _unique(record.split for record in records),
        "draw_seed": _unique(record.draw_seed for record in records),
        "prompt_id": question_sets[0] if len(question_sets) == 1 else None,
        "question_sets": question_sets,
        "metric_id": metric_id_for("reviewer_class_accuracy"),
        "scorer_version": SCORER_VERSION,
        "serving_kind": _unique(record.serving_kind for record in records),
        "model_id": models[0] if len(models) == 1 else None,
        "reviewer_models": models,
    }


def _core(
    records: Sequence[ReviewRecord], *, risk: float, confidence: float, target_accuracy: float
) -> dict[str, Any]:
    """Compute every non-grouped section for one population."""
    class_metrics = _class_metrics(records)
    subclass = _subclass_metrics(records)
    verdict = _verdict_metrics(records)
    acceptance = _acceptance_metrics(records, risk=risk, confidence=confidence)
    routing = _routing_metrics(records)
    cost = _cost_metrics(records, n_accepted=acceptance["n_accepted"])
    return {
        "n": len(records),
        "quality": {
            **class_metrics,
            "subclass": subclass,
            "verdict": verdict,
        },
        "acceptance": acceptance,
        "calibration": _calibration_metrics(records),
        "routing": routing,
        "cost": cost,
        "operating_points": _operating_points(
            records,
            target_accuracy=target_accuracy,
            risk=risk,
            confidence=confidence,
        ),
    }


def _empty_scorecard() -> dict[str, Any]:
    """The zero-record shape, with nulls instead of invented zeros."""
    return {
        "n": 0,
        "quality": {
            "class_accuracy": None,
            "class_accuracy_ci": None,
            "class_macro_accuracy": None,
            "n_expected": 0,
            "n_scored": 0,
            "n_unanswered": 0,
            "coverage": None,
            "correct": 0,
            "per_class": {},
            "confusion": {"matrix": [], "labels": []},
            "subclass": {
                "subclass_correct": 0,
                "subclass_scored": 0,
                "subclass_accuracy": None,
                "by_class": {},
                "accepted_subclass_scored": 0,
                "accepted_subclass_wrong": 0,
                "accepted_subclass_wrong_rate": None,
            },
            "verdict": {
                "verdict_accuracy": None,
                "n": 0,
                "confusion": {"matrix": [], "labels": []},
                "verdicts_by_expected": {},
                "human_verdicts": {},
            },
        },
        "acceptance": {
            "n_accepted": 0,
            "n_accepted_with_truth": 0,
            "n_accepted_without_truth": 0,
            "n_accepted_unanswered": 0,
            "n_wrong_accepts": 0,
            "wrong_accept_rate": None,
            "wrong_accept_upper_95": None,
            "accepted_accuracy": None,
            "required_accepts": None,
            "sample_shortfall": None,
            "meets_risk_bar": False,
        },
        "calibration": {
            "n": 0,
            "ece": None,
            "brier": None,
            "bins": [],
            "mean_confidence": None,
            "mean_confidence_correct": None,
            "mean_confidence_wrong": None,
        },
        "routing": {
            "n_reached": 0,
            "n_human_review": 0,
            "routed_to_human_review": 0,
            "routed_to_human_review_rate": None,
            "routed_to_human_review_by_class": {},
            "routed_to_human_review_basis": "reviewer",
            "cause_counts": {},
            "cause_rates": {},
            "accept_rule_violations": [],
            "accept_rule_ok": True,
        },
        "cost": {
            "currency": "USD",
            "n_priced": 0,
            "n_unpriced": 0,
            "complete": False,
            "priced_total_usd": None,
            "total_usd": None,
            "per_document_usd": None,
            "per_accepted_usd": None,
        },
        "operating_points": {},
    }


def score_review_lane(
    records: Iterable[ReviewRecord | Mapping[str, Any]],
    *,
    risk: float = 0.01,
    confidence: float = 0.95,
    target_accuracy: float = 0.95,
) -> dict[str, Any]:
    """Score one reviewer population and return a JSON-serializable scorecard.

    ``risk`` is the maximum wrong auto-accept rate; the exact one-sided upper
    bound at ``confidence`` decides ``meets_risk_bar``. ``target_accuracy`` is
    the cutoff-tuning target reported in the operating points. Records may be
    :class:`ReviewRecord` objects or mappings accepted by
    :meth:`ReviewRecord.from_mapping`.
    """
    if not 0.0 < risk < 1.0:
        raise ValueError(f"risk must be in (0, 1); got {risk!r}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1); got {confidence!r}")
    if not 0.0 < target_accuracy <= 1.0:
        raise ValueError(f"target_accuracy must be in (0, 1]; got {target_accuracy!r}")
    normalized = [
        record if isinstance(record, ReviewRecord) else ReviewRecord.from_mapping(record)
        for record in records
    ]
    if not normalized:
        payload = _empty_scorecard()
        # Same acceptance keys as a non-empty card: zero accepts still need
        # the zero-error sample size to clear the bar.
        needed = required_samples(0, risk=risk, confidence=confidence)
        payload["acceptance"].update(
            {
                "required_accepts": needed,
                "sample_shortfall": needed,
                "risk": risk,
                "confidence": confidence,
            }
        )
    else:
        payload = _core(
            normalized,
            risk=risk,
            confidence=confidence,
            target_accuracy=target_accuracy,
        )
    by_model: dict[str, Any] = {}
    grouped_model: dict[str, list[ReviewRecord]] = defaultdict(list)
    for record in normalized:
        grouped_model[_label(record.reviewer_model or record.model_id) or "unknown"].append(record)
    for name, rows in sorted(grouped_model.items()):
        by_model[name] = _core(
            rows, risk=risk, confidence=confidence, target_accuracy=target_accuracy
        )
    by_question_set: dict[str, Any] = {}
    grouped_question: dict[str, list[ReviewRecord]] = defaultdict(list)
    for record in normalized:
        grouped_question[_label(record.question_set) or "unknown"].append(record)
    for name, rows in sorted(grouped_question.items()):
        by_question_set[name] = _core(
            rows, risk=risk, confidence=confidence, target_accuracy=target_accuracy
        )
    routing = payload["routing"]
    subclass = payload["quality"]["subclass"]
    verdict = payload["quality"]["verdict"]
    payload.update(
        {
            "routed_to_human_review": routing["routed_to_human_review"],
            "routed_to_human_review_rate": routing["routed_to_human_review_rate"],
            "routed_to_human_review_by_class": routing["routed_to_human_review_by_class"],
            "routed_to_human_review_basis": "reviewer",
            "subclass_accuracy": subclass["subclass_accuracy"],
            "subclass_correct": subclass["subclass_correct"],
            "subclass_scored": subclass["subclass_scored"],
            "verdicts_by_expected": verdict["verdicts_by_expected"],
            "by_model": by_model,
            "by_question_set": by_question_set,
            "provenance": _provenance(normalized),
        }
    )
    return payload
