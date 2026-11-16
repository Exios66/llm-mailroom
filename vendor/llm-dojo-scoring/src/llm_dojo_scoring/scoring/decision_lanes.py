"""Decision-lane scoring: judge, arbiter, boss and the human-review tray.

After classification and extraction the mailroom pipeline makes routing
decisions: the judge rates an extraction, the arbiter accepts it, retries it or
parks it, the boss adjudicates conflicts, and a person resolves what is parked
in the review tray. Each decision is right or wrong only against an outcome, so
this module derives the outcome (the *gold*) for every decision and reports how
often the decision matched it. All functions are pure and network-free.

Input is a list of mailroom ``RunRecord``-shaped mappings (``model_dump()`` of
``schemas/run_record.py``). The fields read, each also accepted as a flat key:

* ``gold.label_source`` (``"human"`` | ``"corpus"``), ``gold.human_resolution``
  (``approve_as_is`` | ``correct`` | ``reclassify`` | ``reject``),
  ``gold.field_corrections`` (or ``gold.human_fields``) and
  ``gold.human_reject_reason``;
* llm-mailroom's ``RunRecord.gold`` shape, read when ``human_resolution`` is
  absent (#159): ``human_decision`` ``rejected`` is ``reject``; ``approved`` is
  ``reclassify`` when ``human_doc_type`` names a class other than the final
  extraction's, else ``approve_as_is`` (a ``correct`` when ``human_fields``
  records edits). ``RunRecord`` keeps no ``removed_fields``, so a review that
  only removed fields reads as ``approve_as_is``;
* ``extraction[]``: ``attempt``, ``doc_type`` and ``extraction_score`` (the
  offline dataset score of that attempt);
* ``judge``: ``ran``, ``verdict``, ``score``, ``on_attempt``, ``doc_type``;
* ``arbiters[]``: ``decision``, ``on_attempt``, ``doc_type``;
* ``boss.decision`` (``approved`` | ``review``);
* ``actual.review_causes`` and ``actual.final_stage``.

Gold, in priority order, recorded per scored row in
``provenance["gold_source"]``:

1. ``human``: the person's resolution in the review tray, used when
   ``label_source`` is ``"human"`` or absent. ``approve_as_is`` that also
   records removed or corrected fields counts as ``correct``. It grades a judge
   verdict or arbiter ruling only when that decision observed the class the
   person resolved; one on another class's extraction is graded by the corpus,
   and so is a typed one when the resolved class is unknown and the final
   attempt has several classes (an untyped decision observed the final
   attempt's class, if it has one);
2. ``corpus``: the deterministic extraction score against dataset labels, read
   through the verdict band ``[low, high]`` (default
   :func:`~llm_dojo_scoring.scoring.field_scoring.get_ambiguous_band`, the band
   :func:`~llm_dojo_scoring.pipeline.pipeline_verdict.deterministic_verdict_label`
   uses: ``complete`` at or above ``high``, ``incomplete`` at or below ``low``).
   Only the final extraction attempt grades a decision about it: when that
   attempt has no score the row has no gold, whatever an earlier attempt scored;
3. none: the row is counted under ``n_unscored_by_reason``. It is never scored 0.
   ``ambiguous_attempt`` is the reason when a decision names an attempt (or the
   final one) that has extractions of several classes and no ``doc_type`` says
   which one it observed. A decision that names its class is graded by that
   class's extraction, never by an untyped one beside it, and its final score
   is the record's last attempt, never an older attempt of its class.

Every scorecard carries ``n``, ``n_scored``, ``n_unscored_by_reason``
(``n == n_scored + sum(n_unscored_by_reason.values())``), a ``metrics`` dict
with one entry per registered ``decision.*`` metric (``None`` when its
denominator is empty), and ``provenance``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..metrics.scorecard_honesty import SCORER_VERSION, metric_id_for
from .classification import macro_prf
from .field_scoring import get_ambiguous_band

__all__ = [
    "ARBITER_DECISIONS",
    "BOSS_DECISIONS",
    "DEFAULT_RETRY_EPSILON",
    "JUDGE_VERDICTS",
    "RESOLUTIONS",
    "score_arbiter_lane",
    "score_boss_lane",
    "score_judge_lane",
    "score_review_tray",
]

#: Judge verdicts that grade an extraction (``judge_error`` is a failed run).
JUDGE_VERDICTS: tuple[str, ...] = ("complete", "partial", "incomplete")
#: The arbiter's three rulings (``agents/arbiter`` in llm-mailroom).
ARBITER_DECISIONS: tuple[str, ...] = ("accept_with_caveats", "retry_extraction", "human_review")
#: The boss's two rulings (``review_decision`` in llm-mailroom).
BOSS_DECISIONS: tuple[str, ...] = ("approved", "review")
#: The person's resolutions (``review_resolve.RESOLUTIONS`` in llm-mailroom).
RESOLUTIONS: tuple[str, ...] = ("approve_as_is", "correct", "reclassify", "reject")
#: Minimum dataset-score rise for a retry to count as useful.
DEFAULT_RETRY_EPSILON = 0.05

#: Resolutions in which the person changed or refused the model's work.
_CHANGED = frozenset({"correct", "reclassify", "reject"})
#: Extraction work the person reviewed field by field.
_FIELD_REVIEWED = frozenset({"approve_as_is", "correct"})
#: llm-mailroom ``RunRecord.gold.human_decision`` values (``export_review_labels``).
_MAILROOM_DECISIONS = frozenset({"approved", "rejected"})

_JUDGE_METRICS = {
    "verdict_accuracy": "judge_verdict_accuracy",
    "macro_f1": "judge_verdict_macro_f1",
    "escalation_recall": "judge_escalation_recall",
    "false_escalation_rate": "judge_false_escalation_rate",
    "error_rate": "judge_error_rate",
}
_ARBITER_METRICS = {
    "decision_accuracy": "arbiter_decision_accuracy",
    "retry_usefulness": "arbiter_retry_usefulness",
    "wrongful_accept_rate": "arbiter_wrongful_accept_rate",
    "unnecessary_escalation_rate": "arbiter_unnecessary_escalation_rate",
}
_BOSS_METRICS = {
    "wrongful_approve_rate": "boss_wrongful_approve_rate",
    "needless_review_rate": "boss_needless_review_rate",
}
_TRAY_METRICS = {
    "wasted_review_rate": "review_tray_wasted_review_rate",
    "correction_rate": "review_tray_correction_rate",
    "reject_rate": "review_tray_reject_rate",
}


# ---------------------------------------------------------------------------
# Reading a RunRecord-shaped mapping
# ---------------------------------------------------------------------------


def _label(value: Any) -> str | None:
    """Lower-cased, stripped label, or ``None`` when blank."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _number(value: Any) -> float | None:
    """A finite float for numeric input (booleans and NaN excluded), else ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _section(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """The nested mapping ``row[key]``, or an empty one."""
    value = row.get(key)
    return value if isinstance(value, Mapping) else {}


def _pick(row: Mapping[str, Any], section: str, *keys: str) -> Any:
    """First present value among ``row[section][key]`` and flat ``row[key]``."""
    nested = _section(row, section)
    for key in keys:
        for source in (nested, row):
            if source.get(key) is not None:
                return source[key]
    return None


@dataclass(frozen=True)
class _Row:
    """One document's decisions and the outcomes that can grade them."""

    doc_id: str
    resolution: str | None
    corrections: dict[str, Any]
    reject_reason: str | None
    attempts: tuple[tuple[int, str | None, float | None], ...]
    human_doc_type: str | None
    judge_verdict: str | None
    judge_on_attempt: int | None
    judge_doc_type: str | None
    rulings: tuple[tuple[str | None, int | None, str | None], ...]
    boss_decision: str | None
    review_causes: tuple[str, ...]
    final_stage: str | None
    dataset_revision: str | None
    split: str | None

    @property
    def outcome(self) -> str | None:
        """The person's resolution, read by what they did to the fields.

        ``approve_as_is`` that also records removed or corrected fields is a
        ``correct``: the person changed the work whatever the label says.
        """
        if self.resolution == "approve_as_is" and self.corrections:
            return "correct"
        return self.resolution

    @property
    def human_changed(self) -> bool:
        """True when the person changed or refused the model's work."""
        return self.outcome in _CHANGED

    def _at(self, attempt: int | None, doc_type: str | None) -> list[tuple[str | None, float | None]]:
        """``(class, score)`` of the extractions of ``attempt`` (``None``: the final one)."""
        applicable = [
            (number, kind, score)
            for number, kind, score in self.attempts
            if doc_type is None or kind is None or kind == doc_type
        ]
        if attempt is None:
            # The final attempt of the record, not the last one of ``doc_type``:
            # an earlier attempt's score never grades the final extraction.
            attempt = max((number for number, _kind, _score in self.attempts), default=None)
        matches = [(kind, score) for number, kind, score in applicable if number == attempt]
        if doc_type is not None:
            # An extraction of ``doc_type`` wins over an untyped one on the same
            # attempt, so list order never picks the score.
            exact = [(kind, score) for kind, score in matches if kind == doc_type]
            return exact or matches
        return matches

    def ambiguous(self, attempt: int | None, doc_type: str | None) -> bool:
        """True when ``attempt`` has extractions of several classes and ``doc_type`` is unknown.

        Taking the first match would let list order decide the gold (#160).
        """
        if doc_type is not None:
            return False
        # An untyped extraction next to a typed one is ambiguous too.
        return len({kind for kind, _score in self._at(attempt, None)}) > 1

    def score_of(self, attempt: int, doc_type: str | None = None) -> float | None:
        """Dataset score of extraction ``attempt`` (of ``doc_type`` when both are known)."""
        matches = self._at(attempt, doc_type)
        return matches[0][1] if matches else None

    def final_score(self, doc_type: str | None = None) -> float | None:
        """Dataset score of the final extraction attempt (of ``doc_type`` if known).

        ``None`` when that attempt has no score: an earlier attempt's score does
        not grade the final extraction.
        """
        matches = self._at(None, doc_type)
        return matches[0][1] if matches else None

    @property
    def resolved_doc_type(self) -> str | None:
        """The class of the extraction the person resolved, when it is known.

        A ``reclassify`` resolved the final extraction's class (``human_doc_type``
        is the new class); any other resolution confirmed ``human_doc_type``.
        """
        final = {kind for kind, _score in self._at(None, None) if kind is not None}
        final_kind = next(iter(final)) if len(final) == 1 else None
        if self.resolution == "reclassify":
            return final_kind
        return self.human_doc_type or final_kind

    def human_applies_to(self, doc_type: str | None) -> bool:
        """True when human gold may grade a decision that observed ``doc_type``."""
        final = {kind for kind, _score in self._at(None, None) if kind is not None}
        if doc_type is None:
            # An untyped decision observed the final attempt's class, if it has one.
            if not final:
                return True
            if len(final) > 1:
                return False
            doc_type = next(iter(final))
        resolved = self.resolved_doc_type
        if resolved is not None:
            return doc_type == resolved
        # The resolved class is unknown. Apply human gold only when nothing
        # shows another class: a final attempt with several typed classes
        # leaves the person's class open, so the corpus grades the decision.
        return not final


def _read(raw: Mapping[str, Any], index: int) -> _Row:
    """Normalise one RunRecord-shaped mapping."""
    if not isinstance(raw, Mapping):
        raise TypeError(f"record {index} must be a mapping; got {type(raw).__name__}")
    source = _label(_pick(raw, "gold", "label_source"))
    resolution = _label(_pick(raw, "gold", "human_resolution", "resolution"))
    human_doc_type = _label(_pick(raw, "gold", "human_doc_type"))
    corrections = _pick(raw, "gold", "field_corrections", "human_fields")
    removed = _pick(raw, "gold", "removed_fields")
    if isinstance(removed, (list, tuple)) and removed:
        corrections = dict(corrections) if isinstance(corrections, Mapping) else {}
        corrections.update({str(name): None for name in removed if str(name) not in corrections})
    attempts = []
    for position, item in enumerate(raw.get("extraction") or ()):
        if not isinstance(item, Mapping):
            continue
        number = item.get("attempt")
        number = number if isinstance(number, int) and not isinstance(number, bool) else position + 1
        attempts.append((number, _label(item.get("doc_type")), _number(item.get("extraction_score"))))
    if resolution is None:
        resolution = _mailroom_resolution(_label(_pick(raw, "gold", "human_decision")), human_doc_type, attempts)
    if resolution not in RESOLUTIONS or source == "corpus":
        resolution = None  # an explicit corpus label is not the person's outcome
    judge = _section(raw, "judge")
    ran = judge.get("ran")
    verdict = _label(_pick(raw, "judge", "verdict", "judge_verdict")) if ran is not False else None
    if verdict is None and ran is True:
        verdict = "judge_error"  # the judge ran and returned nothing
    rulings = []
    for item in raw.get("arbiters") or ():
        if not isinstance(item, Mapping) or item.get("ran") is False:
            continue
        on_attempt = item.get("on_attempt")
        on_attempt = on_attempt if isinstance(on_attempt, int) and not isinstance(on_attempt, bool) else None
        rulings.append((_label(item.get("decision")), on_attempt, _label(item.get("doc_type"))))
    flat_ruling = _label(raw.get("arbiter_decision"))
    if not rulings and flat_ruling:
        rulings.append((flat_ruling, None, None))
    boss = _pick(raw, "boss", "decision", "boss_decision")
    causes = _pick(raw, "actual", "review_causes")
    run = _section(raw, "run")
    judged = judge.get("on_attempt")
    return _Row(
        doc_id=str(raw.get("doc_id") or f"row-{index}"),
        resolution=resolution,
        corrections=dict(corrections) if isinstance(corrections, Mapping) else {},
        reject_reason=_label(_pick(raw, "gold", "human_reject_reason", "reject_reason")),
        attempts=tuple(attempts),
        human_doc_type=human_doc_type,
        judge_verdict=verdict,
        judge_on_attempt=judged if isinstance(judged, int) and not isinstance(judged, bool) else None,
        judge_doc_type=_label(judge.get("doc_type")),
        rulings=tuple(rulings),
        boss_decision=_label(boss),
        review_causes=tuple(str(c).strip() for c in causes if str(c).strip())
        if isinstance(causes, (list, tuple))
        else (),
        final_stage=_label(_pick(raw, "actual", "final_stage")),
        dataset_revision=run.get("dataset_revision") or raw.get("dataset_revision"),
        split=run.get("split") or raw.get("split"),
    )


def _mailroom_resolution(
    decision: str | None,
    human_doc_type: str | None,
    attempts: list[tuple[int, str | None, float | None]],
) -> str | None:
    """Map llm-mailroom's ``human_decision`` onto a resolution (#159).

    ``rejected`` is ``reject``. ``approved`` is ``reclassify`` when the person
    named a class other than the final extraction's, else ``approve_as_is``;
    field edits turn that into ``correct`` through :attr:`_Row.outcome`.
    """
    if decision not in _MAILROOM_DECISIONS:
        return None
    if decision == "rejected":
        return "reject"
    last = max((number for number, _kind, _score in attempts), default=None)
    final = {kind for number, kind, _score in attempts if number == last and kind is not None}
    if human_doc_type and len(final) == 1 and human_doc_type not in final:
        return "reclassify"
    return "approve_as_is"


def _rows(records: Iterable[Mapping[str, Any]]) -> list[_Row]:
    """Normalise every record."""
    return [_read(raw, index) for index, raw in enumerate(records)]


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _rate(hits: int, total: int) -> float | None:
    """``hits / total`` rounded to 4 places; ``None`` when ``total`` is 0."""
    return round(hits / total, 4) if total else None


def _band(band: tuple[float, float] | None) -> tuple[float, float]:
    """Validate ``(low, high)``; ``None`` reads the configured ambiguous band."""
    low, high = band if band is not None else get_ambiguous_band()
    low, high = float(low), float(high)
    if not 0.0 <= low < high <= 1.0:
        raise ValueError(f"band must satisfy 0 <= low < high <= 1; got {(low, high)!r}")
    return low, high


def _unique(values: Iterable[Any]) -> list[str]:
    """Sorted distinct non-empty values as strings."""
    return sorted({str(v) for v in values if v})


def _card(
    rows: list[_Row],
    unscored: Counter,
    sources: Counter,
    metrics: Mapping[str, Any],
    names: Mapping[str, str],
    *,
    units: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The common scorecard envelope; ``units`` overrides ``n`` when it is not one per record."""
    n = len(rows) if units is None else units
    return {
        "n": n,
        "n_scored": n - sum(unscored.values()),
        "n_unscored_by_reason": dict(sorted(unscored.items())),
        "metrics": dict(metrics),
        **extra,
        "provenance": {
            "gold_source": dict(sorted(sources.items())),
            "dataset_revision": _unique(r.dataset_revision for r in rows),
            "split": _unique(r.split for r in rows),
            "metric_ids": {key: metric_id_for(name) for key, name in names.items()},
            "scorer_version": SCORER_VERSION,
        },
    }


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def _expected_verdict(row: _Row, low: float, high: float) -> tuple[str | None, str]:
    """The verdict the judge should have given and where that gold comes from.

    Human: ``approve_as_is`` is ``complete``, ``correct`` is ``partial``,
    ``reject`` and ``reclassify`` are ``incomplete``. The person resolved the
    final extraction, so human gold applies only when the judge saw that
    attempt; a verdict on an earlier, retried attempt is graded by the corpus.
    Corpus: the score of the judged extraction at or above ``high`` is
    ``complete``, at or below ``low`` ``incomplete``, between ``partial``.
    """
    human = {
        "approve_as_is": "complete",
        "correct": "partial",
        "reject": "incomplete",
        "reclassify": "incomplete",
    }.get(row.outcome or "")
    last = max((number for number, _kind, _score in row.attempts), default=None)
    judged_final = row.judge_on_attempt is None or last is None or row.judge_on_attempt >= last
    if row.ambiguous(row.judge_on_attempt, row.judge_doc_type):
        return None, "ambiguous_attempt"
    if human and judged_final and row.human_applies_to(row.judge_doc_type):
        return human, "human"
    if row.judge_on_attempt is not None:
        score = row.score_of(row.judge_on_attempt, row.judge_doc_type)
    else:
        score = row.final_score(row.judge_doc_type)
    if score is None:
        return None, "no_gold"
    if score >= high:
        return "complete", "corpus"
    if score <= low:
        return "incomplete", "corpus"
    return "partial", "corpus"


def score_judge_lane(
    records: Iterable[Mapping[str, Any]],
    *,
    band: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Score the completeness judge against human or corpus gold.

    ``escalation_recall`` is the share of documents that were not complete in
    gold whose verdict was not ``complete``; ``false_escalation_rate`` the share
    of complete ones the judge did not call complete. ``macro_f1`` is the
    one-vs-rest macro F1 over the gold verdict classes. A ``judge_error``
    verdict is counted under ``unscored`` and in ``error_rate`` (errors over
    every judge run), and is left out of the other metrics.
    """
    low, high = _band(band)
    rows = _rows(records)
    unscored: Counter = Counter()
    sources: Counter = Counter()
    gold_labels: list[str] = []
    predicted: list[str] = []
    ran = errors = 0
    for row in rows:
        verdict = row.judge_verdict
        if verdict is None:
            unscored["judge_not_run"] += 1
            continue
        ran += 1
        if verdict == "judge_error":
            errors += 1
            unscored["judge_error"] += 1
            continue
        if verdict not in JUDGE_VERDICTS:
            unscored["unknown_verdict"] += 1
            continue
        gold, source = _expected_verdict(row, low, high)
        if gold is None:
            unscored[source] += 1
            continue
        sources[source] += 1
        gold_labels.append(gold)
        predicted.append(verdict)
    pairs = list(zip(gold_labels, predicted))
    not_complete = [p for g, p in pairs if g != "complete"]
    complete = [p for g, p in pairs if g == "complete"]
    metrics = {
        "verdict_accuracy": _rate(sum(g == p for g, p in pairs), len(pairs)),
        "macro_f1": (
            round(macro_prf(gold_labels, predicted, normalize=False)["f1_macro"], 4) if pairs else None
        ),
        "escalation_recall": _rate(sum(p != "complete" for p in not_complete), len(not_complete)),
        "false_escalation_rate": _rate(sum(p != "complete" for p in complete), len(complete)),
        "error_rate": _rate(errors, ran),
    }
    confusion: dict[str, dict[str, int]] = {}
    for gold, verdict in pairs:
        confusion.setdefault(gold, {}).setdefault(verdict, 0)
        confusion[gold][verdict] += 1
    return _card(
        rows,
        unscored,
        sources,
        metrics,
        _JUDGE_METRICS,
        band=[low, high],
        verdicts_by_expected={k: dict(sorted(v.items())) for k, v in sorted(confusion.items())},
    )


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------


def _arbiter_outcome(
    row: _Row,
    ruling: tuple[str | None, int | None, str | None],
    *,
    high: float,
    epsilon: float,
) -> tuple[bool | None, str]:
    """Whether one ruling was right, and the gold source or the reason it is unscored.

    * ``retry_extraction``: right when the next attempt's dataset score rose by
      at least ``epsilon`` over the attempt that was ruled on (corpus only: a
      person's later approval does not show the retry helped).
    * ``accept_with_caveats``: right when the person approved as is, else, with
      no resolution, when the final dataset score is at or above ``high``.
    * ``human_review``: right (needed) when the person changed or refused the
      work, else, with no resolution, when the final score is below ``high``;
      wrong (unnecessary) when the person approved as is.
    """
    decision, on_attempt, doc_type = ruling
    if decision not in ARBITER_DECISIONS:
        return None, "unknown_decision"
    if decision == "retry_extraction":
        attempt = on_attempt if on_attempt is not None else 1
        if row.ambiguous(attempt, doc_type) or row.ambiguous(attempt + 1, doc_type):
            return None, "ambiguous_attempt"
        before, after = row.score_of(attempt, doc_type), row.score_of(attempt + 1, doc_type)
        if before is None or after is None:
            return None, "no_retry_scores"
        # Rounded: 0.85 - 0.80 is 0.04999999999999993 in floats.
        return round(after - before, 9) >= epsilon, "corpus"
    if row.ambiguous(None, doc_type):
        return None, "ambiguous_attempt"
    if row.resolution is not None and row.human_applies_to(doc_type):
        if decision == "accept_with_caveats":
            return row.outcome == "approve_as_is", "human"
        return row.human_changed, "human"
    score = row.final_score(doc_type)
    if score is None:
        return None, "no_gold"
    return (score >= high if decision == "accept_with_caveats" else score < high), "corpus"


def score_arbiter_lane(
    records: Iterable[Mapping[str, Any]],
    *,
    epsilon: float = DEFAULT_RETRY_EPSILON,
    band: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Score the extraction arbiter's rulings.

    The unit is a ruling: a document the arbiter ruled on twice (retry, then
    accept) counts twice, and a document with no ruling counts once as
    ``arbiter_not_run``, so ``n`` is the number of units. ``retry_usefulness``
    is the share of scored retries whose next attempt rose by at least
    ``epsilon``; ``wrongful_accept_rate`` the share of scored accepts that were
    wrong; ``unnecessary_escalation_rate`` the share of scored
    ``human_review`` rulings that were not needed.
    """
    if not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError(f"epsilon must be a non-negative number; got {epsilon!r}")
    low, high = _band(band)
    rows = _rows(records)
    unscored: Counter = Counter()
    sources: Counter = Counter()
    per_decision: dict[str, Counter] = {}
    units = 0
    for row in rows:
        if not row.rulings:
            units += 1
            unscored["arbiter_not_run"] += 1
            continue
        for ruling in row.rulings:
            units += 1
            right, source = _arbiter_outcome(row, ruling, high=high, epsilon=epsilon)
            if right is None:
                unscored[source] += 1
                continue
            sources[source] += 1
            per_decision.setdefault(str(ruling[0]), Counter())["right" if right else "wrong"] += 1

    def count(decision: str, key: str) -> int:
        """Scored rulings of ``decision`` with outcome ``key``."""
        return per_decision.get(decision, Counter())[key]

    def scored(decision: str) -> int:
        """All scored rulings of ``decision``."""
        return count(decision, "right") + count(decision, "wrong")

    right_total = sum(c["right"] for c in per_decision.values())
    scored_total = sum(c["right"] + c["wrong"] for c in per_decision.values())
    metrics = {
        "decision_accuracy": _rate(right_total, scored_total),
        "retry_usefulness": _rate(count("retry_extraction", "right"), scored("retry_extraction")),
        "wrongful_accept_rate": _rate(count("accept_with_caveats", "wrong"), scored("accept_with_caveats")),
        "unnecessary_escalation_rate": _rate(count("human_review", "wrong"), scored("human_review")),
    }
    return _card(
        rows,
        unscored,
        sources,
        metrics,
        _ARBITER_METRICS,
        units=units,
        epsilon=epsilon,
        band=[low, high],
        by_decision={k: dict(v) for k, v in sorted(per_decision.items())},
    )


# ---------------------------------------------------------------------------
# Boss
# ---------------------------------------------------------------------------


def score_boss_lane(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score the boss's approve / review rulings against the person's resolution.

    Only a human outcome grades the boss, so a row without a resolution is
    ``no_boss_gold``. ``approved`` is wrongful when the person later changed or
    refused the work; ``review`` is needless when the person approved as is.
    Approved documents reach a person only when they are sampled for audit, so
    ``wrongful_approve_rate`` describes the audited ones.
    """
    rows = _rows(records)
    unscored: Counter = Counter()
    sources: Counter = Counter()
    approved = wrongful = reviewed = needless = 0
    for row in rows:
        decision = row.boss_decision
        if decision is None:
            unscored["boss_not_run"] += 1
        elif decision not in BOSS_DECISIONS:
            unscored["unknown_decision"] += 1
        elif row.resolution is None:
            unscored["no_boss_gold"] += 1
        else:
            sources["human"] += 1
            if decision == "approved":
                approved += 1
                wrongful += int(row.human_changed)
            else:
                reviewed += 1
                needless += int(row.outcome == "approve_as_is")
    metrics = {
        "wrongful_approve_rate": _rate(wrongful, approved),
        "needless_review_rate": _rate(needless, reviewed),
    }
    return _card(
        rows,
        unscored,
        sources,
        metrics,
        _BOSS_METRICS,
        by_decision={
            "approved": {"n": approved, "wrongful": wrongful},
            "review": {"n": reviewed, "needless": needless},
        },
    )


# ---------------------------------------------------------------------------
# Review tray
# ---------------------------------------------------------------------------


def score_review_tray(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score what people did with the documents parked in the review tray.

    A row is resolved when it carries a human resolution. A row without one is
    ``unresolved`` when it is still parked (``final_stage`` ``review``) and
    ``not_parked`` otherwise. For resolved rows it reports the resolution mix (the
    person's label as given), ``wasted_review_rate`` (approved as is with no field corrections: the review
    changed nothing), ``correction_rate`` (``correct``, where an ``approve_as_is`` that
    records field edits counts as ``correct``), ``reject_rate``, the reject-reason
    distribution, ``field_correction_rate`` (per field, over the reviews that
    checked fields: ``approve_as_is`` and ``correct``) and ``by_review_cause``
    (per cause, the share of its parks where the person changed the work).
    """
    rows = _rows(records)
    unscored: Counter = Counter()
    resolved = []
    for row in rows:
        if row.resolution is not None:
            resolved.append(row)
        elif row.final_stage == "review":
            unscored["unresolved"] += 1
        else:
            unscored["not_parked"] += 1
    mix = Counter(row.resolution for row in resolved)  # the person's label, as given
    outcomes = Counter(row.outcome for row in resolved)  # an edited approve_as_is is a correct
    wasted = outcomes["approve_as_is"]
    reasons = Counter(row.reject_reason or "unspecified" for row in resolved if row.resolution == "reject")
    field_reviews = [row for row in resolved if row.resolution in _FIELD_REVIEWED]
    field_edits = Counter(str(name) for row in field_reviews for name in row.corrections)
    cause_total: Counter = Counter()
    cause_changed: Counter = Counter()
    for row in resolved:
        for cause in set(row.review_causes):
            cause_total[cause] += 1
            cause_changed[cause] += int(row.human_changed)
    metrics = {
        "wasted_review_rate": _rate(wasted, len(resolved)),
        "correction_rate": _rate(outcomes["correct"], len(resolved)),
        "reject_rate": _rate(mix["reject"], len(resolved)),
    }
    return _card(
        rows,
        unscored,
        Counter({"human": len(resolved)} if resolved else {}),
        metrics,
        _TRAY_METRICS,
        resolutions=dict(sorted(mix.items())),
        reject_reasons=dict(sorted(reasons.items())),
        field_correction_rate={
            name: _rate(count, len(field_reviews)) for name, count in sorted(field_edits.items())
        },
        by_review_cause={
            cause: {
                "n": cause_total[cause],
                "changed": cause_changed[cause],
                "precision": _rate(cause_changed[cause], cause_total[cause]),
            }
            for cause in sorted(cause_total)
        },
    )
