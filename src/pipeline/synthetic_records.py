"""Seeded synthetic ``RunRecord`` generator with a PLANTED calibration curve.

THIS IS NEVER EVIDENCE ABOUT REAL MODELS. It exists only to validate the
hill-climb harness (``pipeline/policy_replay.py``, ``scripts/hillclimb_eval.py``,
``scripts/calibrate_thresholds.py``): because the true ``P(correct | confidence)``
of every class is chosen here, the harness can be checked against a known
answer (does the calibrator recover the planted cutoff, does it refuse an
under-sampled class, do the guards fire on an overconfident region). Every
record is marked ``mode="synthetic"``, ``full_information=True``. Numbers
produced from these records say nothing about any classifier, prompt or model;
real thresholds need real ``RunRecord`` sweeps.

Planted truth
-------------
Per class, ``SORTER_CURVES`` / ``EXTRACTION_CURVES`` give monotone
piecewise-linear knots ``(stated confidence, true P(correct))``. Several have an
overconfident region (true accuracy below the stated confidence just under the
cutoff). ``planted_cutoff`` integrates the curve against the confidence
density on a fine grid and returns the population analogue of the Dojo's
``cutoff_at_target``: the lowest grid confidence whose accepted set reaches the
target accuracy (grid resolution, not exact; see its docstring). Attempts, the reviewer and the judge are independent draws
(a deliberate simplification); the field floor the extraction curve was planted
against is 0.80 (successes score in [0.85, 1.0], failures in [0.20, 0.75]).

CLI::

    PYTHONPATH=src python -m pipeline.synthetic_records --out recs.jsonl --n-per-class 1500
"""

from __future__ import annotations

import argparse
import math
import random
from typing import Mapping, Sequence

from schemas.run_record import (
    ActualRoute,
    ArbiterObservation,
    ExtractionAttempt,
    Gold,
    JudgeObservation,
    Observed,
    ReviewerObservation,
    RunInfo,
    RunRecord,
    SorterAttempt,
)

Knots = tuple[tuple[float, float], ...]

# (stated confidence, true P(class correct)); overconfident in the 0.90-0.95 band.
SORTER_CURVES: dict[str, Knots] = {
    "contract": ((0.55, 0.30), (0.75, 0.55), (0.90, 0.78), (0.95, 0.90), (0.975, 0.985), (1.0, 0.995)),
    "merger_agreement": ((0.55, 0.40), (0.80, 0.70), (0.93, 0.88), (0.96, 0.93), (0.985, 0.99), (1.0, 0.998)),
    "insurance_claim": ((0.55, 0.45), (0.85, 0.80), (0.94, 0.92), (0.97, 0.975), (1.0, 0.995)),
    "corporate_record": ((0.55, 0.25), (0.80, 0.50), (0.90, 0.72), (0.96, 0.86), (0.99, 0.97), (1.0, 0.99)),
    "correspondence": ((0.55, 0.50), (0.85, 0.85), (0.92, 0.94), (1.0, 0.99)),
}

# (stated extraction confidence, true P(extraction_score >= 0.80))
EXTRACTION_CURVES: dict[str, Knots] = {
    "contract": ((0.50, 0.20), (0.80, 0.45), (0.92, 0.70), (0.97, 0.93), (1.0, 0.985)),
    "merger_agreement": ((0.50, 0.15), (0.85, 0.50), (0.95, 0.80), (0.98, 0.955), (1.0, 0.99)),
    "insurance_claim": ((0.50, 0.30), (0.85, 0.75), (0.93, 0.93), (1.0, 0.99)),
    "corporate_record": ((0.50, 0.20), (0.85, 0.55), (0.93, 0.78), (0.97, 0.94), (1.0, 0.985)),
    "correspondence": ((0.50, 0.35), (0.80, 0.80), (0.90, 0.95), (1.0, 0.995)),
}

CONF_MIN = {"sorter": 0.55, "extraction": 0.50}
CONF_SHAPE = {"sorter": 2.5, "extraction": 2.0}  # larger = more mass near 1.0

COST_USD = {"sorter": 0.004, "reviewer": 0.006, "extraction": 0.020, "judge": 0.015, "arbiter": 0.018}
# retry_max (2) confidence retries plus up to arbiter_retry_max (2) arbiter-ordered re-extractions
MAX_EXTRACTION_ATTEMPTS = 5


def true_accuracy(knots: Knots, conf: float) -> float:
    """Planted ``P(correct | conf)`` (piecewise linear, clamped at the ends)."""
    if conf <= knots[0][0]:
        return knots[0][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if conf <= x1:
            return y0 + (y1 - y0) * (conf - x0) / (x1 - x0)
    return knots[-1][1]


def _cdf(kind: str, c: float) -> float:
    lo, shape = CONF_MIN[kind], CONF_SHAPE[kind]
    span = 1.0 - lo
    if c <= lo:
        return 0.0
    if c >= 1.0:
        return 1.0
    return 1.0 - ((1.0 - c) / span) ** (1.0 / shape)


def _draw_conf(rng: random.Random, kind: str) -> float:
    lo, shape = CONF_MIN[kind], CONF_SHAPE[kind]
    return round(1.0 - (1.0 - lo) * rng.random() ** shape, 4)


def planted_cutoff(cls: str, target: float, kind: str = "sorter", *, grid: int = 4000) -> float | None:
    """Population cutoff: lowest grid confidence whose accepted set reaches ``target``.

    A numerical approximation of the population analogue of ``cutoff_at_target``,
    not an exact value: the planted curve is evaluated at each grid cell's
    midpoint and the answer is the left edge of a cell (``grid`` cells over the
    confidence range, so it is within one cell width, ``(1 - lo) / grid``, of the
    continuous cutoff). It returns ``None`` when no cell's accepted set reaches
    ``target``, which includes a target the curve only reaches inside the last
    cell, where the midpoint accuracy can fall just short of it. Raise ``grid``
    to tighten both effects.
    """
    knots = (SORTER_CURVES if kind == "sorter" else EXTRACTION_CURVES)[cls]
    lo = CONF_MIN[kind]
    xs = [lo + (1.0 - lo) * i / grid for i in range(grid + 1)]
    mass = [_cdf(kind, xs[i + 1]) - _cdf(kind, xs[i]) for i in range(grid)]
    mids = [(xs[i] + xs[i + 1]) / 2 for i in range(grid)]
    right = sum_mass = 0.0
    best: float | None = None
    for i in range(grid - 1, -1, -1):  # walk down from confidence 1.0
        right += mass[i] * true_accuracy(knots, mids[i])
        sum_mass += mass[i]
        if sum_mass > 0 and right / sum_mass >= target:
            best = xs[i]
    return best


def planted_set_precision(cls: str, cutoff: float, kind: str = "sorter", *, grid: int = 4000) -> float:
    """True accuracy of the accepted set ``confidence >= cutoff`` under the planted curve."""
    knots = (SORTER_CURVES if kind == "sorter" else EXTRACTION_CURVES)[cls]
    lo = CONF_MIN[kind]
    xs = [max(lo + (1.0 - lo) * i / grid, cutoff) for i in range(grid + 1)]
    right = total = 0.0
    for a, b in zip(xs, xs[1:]):
        w = _cdf(kind, b) - _cdf(kind, a)
        right += w * true_accuracy(knots, (a + b) / 2)
        total += w
    return right / total if total else float("nan")


def planted_accept_share(cls: str, cutoff: float, kind: str = "sorter") -> float:
    """Share of the class's documents with confidence >= ``cutoff``."""
    return 1.0 - _cdf(kind, cutoff)




def _noisy(rng: random.Random, base: float) -> float:
    return round(base * math.exp(rng.gauss(0.0, 0.25)), 6)


def _other(rng: random.Random, classes: Sequence[str], gold: str) -> str:
    return rng.choice([c for c in classes if c != gold])


def _sorter_attempt(
    rng: random.Random, attempt: int, label: str, classes: Sequence[str], *, planted_on: str
) -> tuple[SorterAttempt, bool]:
    """One sorter attempt. ``planted_on`` is the class whose curve decides correctness.

    Attempt 1 is planted on the class the sorter *names* (``label``): that is the
    key the calibrator groups by, so its per-class curve is recovered exactly.
    Returns ``(attempt, label_is_gold)``.
    """
    conf = _draw_conf(rng, "sorter")
    correct = rng.random() < true_accuracy(SORTER_CURVES[planted_on], conf)
    return (
        SorterAttempt(
            attempt=attempt,
            doc_type=label,
            confidence_raw=conf,
            confidence=conf,
            prompt_tokens=1800,
            completion_tokens=60,
            cost_usd=_noisy(rng, COST_USD["sorter"]),
            latency_s=round(_noisy(rng, 1.5), 3),
        ),
        correct,
    )


def _extraction_attempt(rng: random.Random, attempt: int, gold: str) -> tuple[ExtractionAttempt, bool]:

    conf = _draw_conf(rng, "extraction")
    success = rng.random() < true_accuracy(EXTRACTION_CURVES[gold], conf)
    score = round(rng.uniform(0.85, 1.0) if success else rng.uniform(0.20, 0.75), 4)
    chunks = 2 if rng.random() < 0.10 else 1
    hollow = rng.random() < 0.01
    return (
        ExtractionAttempt(
            attempt=attempt,
            confidence_raw=conf,
            confidence=conf,
            schema_valid=rng.random() > 0.03,
            hollow=hollow,
            n_chunks=chunks,
            extraction_score=score,
            hallucination_rate=round(rng.uniform(0.0, 0.05 if success else 0.3), 4),
            prompt_tokens=3500 * chunks,
            completion_tokens=400,
            cost_usd=_noisy(rng, COST_USD["extraction"] * chunks),
            latency_s=round(_noisy(rng, 4.0), 3),
        ),
        success,
    )


def _judge_for_attempt(rng: random.Random, attempt: int, success: bool) -> JudgeObservation:
    complete = rng.random() < (0.85 if success else 0.20)  # a noisy judge
    verdict = "complete" if complete else rng.choice(["partial", "incomplete"])
    return JudgeObservation(
        ran=True,
        on_attempt=attempt,
        verdict=verdict,
        score=round(rng.uniform(0.8, 1.0) if complete else rng.uniform(0.3, 0.7), 4),
        source_verified=complete and success and rng.random() < 0.9,
        prompt_tokens=4200,
        completion_tokens=250,
        cost_usd=_noisy(rng, COST_USD["judge"]),
        latency_s=round(_noisy(rng, 3.0), 3),
    )


def _arbiter_for_attempt(rng: random.Random, attempt: int) -> ArbiterObservation:
    roll = rng.random()
    decision = "accept_with_caveats" if roll < 0.30 else "retry_extraction" if roll < 0.80 else "human_review"
    return ArbiterObservation(
        ran=True,
        on_attempt=attempt,
        decision=decision,
        prompt_tokens=4500,
        completion_tokens=300,
        cost_usd=_noisy(rng, COST_USD["arbiter"]),
        latency_s=round(_noisy(rng, 3.5), 3),
    )


def generate_records(
    n_per_class: int | Mapping[str, int] = 1000,
    *,
    seed: int = 42,
    test_fraction: float = 0.3,
    classes: Sequence[str] | None = None,
) -> list[RunRecord]:
    """Generate full-information synthetic records (deterministic for a seed).

    ``n_per_class`` counts the documents the sorter NAMES as each class on its
    first attempt (the key the calibrator groups by, so that attempt's planted
    curve is recovered exactly); it may be a dict to under-sample a class on
    purpose. Records are shuffled and then split into ``train`` / ``test`` by
    ``test_fraction``. Every attempt, the reviewer, the judge (each attempt) and
    the arbiter (after each non-``complete`` verdict) are observed.
    """
    from pipeline.policy_replay import llm_review_verdict

    names = tuple(classes) if classes is not None else tuple(SORTER_CURVES)
    if len(set(names)) == 1:  # _other() needs a second class to name when the sorter is wrong
        raise ValueError(f"need at least two distinct classes, got {sorted(set(names))}")
    unknown = [c for c in names if c not in SORTER_CURVES]
    if unknown:
        raise ValueError(f"no planted curve for class(es): {unknown}")
    counts = {c: (n_per_class if isinstance(n_per_class, int) else int(n_per_class.get(c, 0))) for c in names}
    rng = random.Random(seed)
    predicted = [c for c in names for _ in range(counts[c])]  # classes the sorter will name
    rng.shuffle(predicted)
    run_id = f"synthetic-seed{seed}"
    records: list[RunRecord] = []
    for index, pred in enumerate(predicted):
        a1, a1_correct = _sorter_attempt(rng, 1, pred, names, planted_on=pred)
        gold = pred if a1_correct else _other(rng, names, pred)
        # Retry: an independent re-roll against the (now known) gold class.
        a2_probe, a2_correct = _sorter_attempt(rng, 2, gold, names, planted_on=gold)
        a2 = a2_probe if a2_correct else a2_probe.model_copy(update={"doc_type": _other(rng, names, gold)})
        sorter = [a1, a2]

        # Reviewer: slightly better than the sorter, own confidence draw.
        r_conf = _draw_conf(rng, "sorter")
        r_ok = rng.random() < min(1.0, true_accuracy(SORTER_CURVES[gold], r_conf) + 0.03)
        r_type = gold if r_ok else _other(rng, names, gold)
        reviewer = ReviewerObservation(
            ran=True,
            mode="llm",
            reviewer_doc_type=r_type,
            reviewer_confidence=r_conf,
            reviewer_verdict=llm_review_verdict(sorter[0].doc_type, r_type, r_conf),
            prompt_tokens=2000,
            completion_tokens=80,
            cost_usd=_noisy(rng, COST_USD["reviewer"]),
            latency_s=round(_noisy(rng, 2.0), 3),
        )

        extraction: list[ExtractionAttempt] = []
        judges: list[JudgeObservation] = []
        arbiters: list[ArbiterObservation] = []
        for k in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
            att, success = _extraction_attempt(rng, k, gold)
            extraction.append(att)
            judge = _judge_for_attempt(rng, k, success)
            judges.append(judge)
            if judge.verdict != "complete":
                arbiters.append(_arbiter_for_attempt(rng, k))

        split = "test" if rng.random() < test_fraction else "train"
        records.append(
            RunRecord(
                run=RunInfo(run_id=run_id, dataset_id="synthetic", split=split, mode="synthetic"),
                doc_id=f"syn-{seed}-{index:06d}",
                filename=f"synthetic_{index:06d}.txt",
                text_chars=5000,
                gold=Gold(expected_doc_type=gold),
                sorter=sorter,
                reviewer=reviewer,
                extraction=extraction,
                judge=judges[-1],
                judges=judges[:-1],
                arbiters=arbiters,
                observed=Observed(
                    sorter_attempts=2,
                    reviewer=True,
                    extraction_attempts=MAX_EXTRACTION_ATTEMPTS,
                    judge=True,
                    full_information=True,
                ),
                actual=ActualRoute(),
            )
        )
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--n-per-class", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--test-fraction", type=float, default=0.3)
    args = parser.parse_args(argv)
    from pipeline.policy_replay import dump_records

    records = generate_records(args.n_per_class, seed=args.seed, test_fraction=args.test_fraction)
    n = dump_records(records, args.out)
    print(f"wrote {n} SYNTHETIC records to {args.out} (harness validation only; not evidence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
