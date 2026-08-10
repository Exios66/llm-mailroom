#!/usr/bin/env python3
"""Calibrate the deterministic field-scoring thresholds (issue #4).

Issue #4's calibration step: take a labeled sample of fields, compute the
deterministic similarity score against a known "is this actually correct"
label, and pick real cutoffs per field type instead of guessing a single
global threshold.

This script builds the labeled sample synthetically from the pipeline's own
ground truth (``examples/samples/manifest.csv`` ``expected_fields``):
for every real expected value it generates a set of controlled perturbations
that simulate realistic extraction outcomes — exact reproductions (correct),
format/typography variants (correct, should score high), and content errors
(incorrect, should score low). Each perturbation is labeled correct/incorrect
*by construction*, so the script can measure how well the deterministic
scorer separates the classes per field type.

Output:
  - per-field-type score distributions for correct vs incorrect predictions
  - suggested ``field_scoring.ambiguous_band`` cutoffs per field type (the
    score below which a field is near-certainly wrong, and the score above
    which it is near-certainly right — everything between should escalate to
    the LLM judge)
  - a verdict on whether the configured global band (0.5–0.85) is sane

Usage:
    python scripts/calibrate_field_scoring.py              # full report
    python scripts/calibrate_field_scoring.py --band 0.5 0.85  # evaluate a specific band
    python scripts/calibrate_field_scoring.py --json       # machine-readable output
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability.field_scoring import (  # noqa: E402
    EntityListScore,
    get_ambiguous_band,
    score_extraction,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"


# ---------------------------------------------------------------------------
# Perturbation generators: one class of *incorrect* variants per field type,
# plus exact/format variants that are *correct*.
# ---------------------------------------------------------------------------

def _perturb_date(value: str) -> list[str]:
    """Incorrect dates: one day off, wrong month, wrong year, swapped parts."""
    import datetime
    import re

    def safe(fn):
        try:
            return fn().strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            dt = datetime.datetime.strptime(value.strip(), fmt).date()
            break
        except ValueError:
            continue
    else:
        return []
    return [
        safe(lambda: dt - datetime.timedelta(days=1)),
        safe(lambda: dt + datetime.timedelta(days=1)),
        safe(lambda: dt.replace(month=(dt.month % 12) + 1, day=1)),
        safe(lambda: dt.replace(year=dt.year + 1)),
    ]


def _perturb_money(value: str) -> list[str]:
    """Incorrect amounts: off by 1, off by an order of magnitude, dropped digit."""
    import re

    m = re.search(r"[-+]?[\d,]+(?:\.\d+)?", value)
    if not m:
        return []
    try:
        num = float(m.group().replace(",", ""))
    except ValueError:
        return []
    return [
        f"{num + 1:.2f}",
        f"{num - 1:.2f}",
        f"{num * 10:.2f}",
        f"{num / 10:.2f}",
    ]


def _perturb_name(value: str) -> list[str]:
    """Genuinely incorrect names: different entity, missing core token,
    single-token typo. (Suffix swaps like Inc./LLC are NOT included — they are
    correct-after-normalize, which is exactly what normalization should do.)"""
    words = value.split()
    return [
        "Another Entity, Inc.",
        " ".join(words[:-1]) if len(words) > 1 else value + " Partners",
        (words[0] + "x" + " " + " ".join(words[1:])) if words else value,
    ]


def _perturb_free_text(value: str) -> list[str]:
    """Incorrect free text: contradictory paraphrase, dropped content, wrong subject."""
    words = value.split()
    n = max(1, len(words) // 3)
    truncated = " ".join(words[: len(words) - n])
    return [
        truncated,
        "unrelated boilerplate that does not answer the question",
        "termination without cause is expressly prohibited in all cases",
    ]


def _perturb_entity_list(value: list) -> list[list]:
    """Incorrect lists: drop one, add one, swap one. (Reordering is NOT
    included — bipartite matching is order-agnostic by design, per issue #4.)"""
    if not value:
        return []
    return [
        value[:-1],
        value + ["Extra Entity, Inc."],
        [value[0] if len(value) > 1 else "Other"] + value[1:],
    ]


def _predictions_for(field_type: str, expected, rng: random.Random):
    """Yield (predicted, label) pairs: one exact + format variants (True),
    plus perturbed incorrect variants (False)."""
    from observability.field_scoring import is_entity_list

    is_list = is_entity_list(field_type)
    elem_type = field_type.split(":", 1)[1] if ":" in field_type else "name"

    if is_list:
        expected_list = expected if isinstance(expected, list) else [expected]
        yield expected_list, True
        for bad in _perturb_entity_list(expected_list):
            yield bad, False
        return

    if expected is None:
        return
    text = str(expected)

    if field_type == "date":
        yield text, True  # exact
        yield text.replace("/", "-"), True  # format variant
        for bad in _perturb_date(text):
            yield bad, False
    elif field_type == "money":
        yield text, True
        yield text.replace(",", ""), True  # punctuation variant
        for bad in _perturb_money(text):
            yield bad, False
    elif field_type in ("name", "id"):
        yield text, True
        yield text.upper(), True  # case variant
        for bad in _perturb_name(text):
            yield bad, False
    else:  # free_text
        yield text, True
        yield " ".join(text.split()), True  # whitespace variant
        # Correct paraphrase: drop parenthetical digits/units, same meaning.
        import re as _re
        para = _re.sub(r"\([^)]*\)", "", text)
        para = _re.sub(r"\s+", " ", para).strip()
        if para and para != text and len(para.split()) >= 3:
            yield para, True
        for bad in _perturb_free_text(text):
            yield bad, False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--band", nargs=2, type=float, metavar=("LOW", "HIGH"),
                    help="ambiguous band to evaluate (default: configured value)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-fields", type=int, default=100,
                    help="cap labeled fields per field type (default 100)")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)

    # 1) Collect the labeled sample: (field_type, expected_value, predicted, correct)
    labeled = []  # (field_type, correct: bool, score: float)
    seen = 0
    with open(MANIFEST, newline="") as fh:
        for row in csv.DictReader(fh):
            expected_fields = row.get("expected_fields")
            if not expected_fields:
                continue
            try:
                expected = json.loads(expected_fields)
            except json.JSONDecodeError:
                continue
            doc_class = row.get("expected_doc_class", "contract")

            from observability.field_scoring import get_field_types
            field_types = get_field_types(doc_class)
            if not field_types:
                continue

            for field, field_type in field_types.items():
                exp_value = expected.get(field)
                if exp_value is None:
                    continue
                for predicted, correct in _predictions_for(field_type, exp_value, rng):
                    if field_type.startswith("entity_list:"):
                        result = score_extraction(
                            doc_class, {field: field_type}, {field: predicted}, {field: exp_value}
                        )
                        score = result.overall_score or 0.0
                    else:
                        result = score_extraction(
                            doc_class, {field: field_type}, {field: predicted}, {field: exp_value}
                        )
                        score = result.overall_score or 0.0
                    labeled.append((field_type, correct, score))
            seen += 1
            if seen >= 100:  # manifest has 30 samples; keep the cap generous
                break

    if not labeled:
        print("no labeled fields produced", file=sys.stderr)
        return 1

    # 2) Per-field-type separation stats.
    per_type: dict[str, dict] = {}
    for ft, correct, score in labeled:
        d = per_type.setdefault(ft, {"correct": [], "incorrect": []})
        d["correct" if correct else "incorrect"].append(score)

    band_low, band_high = args.band or get_ambiguous_band()

    report = {
        "band_evaluated": [band_low, band_high],
        "n_fields": len(labeled),
        "field_types": {},
    }
    for ft in sorted(per_type):
        ok = per_type[ft]["correct"]
        bad = per_type[ft]["incorrect"]
        if not ok or not bad:
            continue

        def q(vals, p):
            if not vals:
                return None
            s = sorted(vals)
            return s[min(len(s) - 1, int(p * (len(s) - 1)))]

        # Suggested band: [max incorrect score seen, min correct score seen].
        # When they overlap (max_incorrect >= min_correct) no deterministic
        # cutoff exists — the field type must always escalate to the LLM judge.
        suggest_low = max(bad) if bad else band_low
        suggest_high = min(ok) if ok else band_high
        separable = suggest_low < suggest_high
        verdict = ("no cutoff — always escalate to judge" if not separable
                   else f"deterministic cutoff {suggest_low:.3f}–{suggest_high:.3f}")
        # Classification accuracy of the configured band on this sample:
        # correct fields scoring >= band_low are trusted; incorrect fields
        # scoring < band_low are caught; everything else escalates to judge.
        trusted_ok = sum(1 for s in ok if s >= band_low)
        caught_bad = sum(1 for s in bad if s < band_low)
        escalations = (
            sum(1 for s in ok if band_low <= s < band_high)
            + sum(1 for s in bad if band_low <= s < band_high)
        )
        report["field_types"][ft] = {
            "n_correct": len(ok),
            "n_incorrect": len(bad),
            "correct_p10_p50_p90": [q(ok, 0.1), q(ok, 0.5), q(ok, 0.9)],
            "incorrect_p10_p50_p90": [q(bad, 0.1), q(bad, 0.5), q(bad, 0.9)],
            "min_correct": min(ok),
            "max_incorrect": max(bad),
            "suggested_band": [round(suggest_low, 3), round(suggest_high, 3)],
            "verdict": verdict,
            "configured_band": {
                "trusted_correct": f"{trusted_ok}/{len(ok)}",
                "caught_incorrect": f"{caught_bad}/{len(bad)}",
                "escalated_to_judge": escalations,
            },
        }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Calibration over {report['n_fields']} labeled fields "
          f"(band evaluated: {band_low}–{band_high})")
    print()
    hdr = (f"{'field type':<22}{'n ok/bad':>10}{'correct p50':>12}{'bad p50':>10}"
           f"{'suggested band':>18}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for ft in sorted(report["field_types"]):
        d = report["field_types"][ft]
        print(
            f"{ft:<22}{str(d['n_correct'])+'/'+str(d['n_incorrect']):>10}"
            f"{d['correct_p10_p50_p90'][1]:>12.3f}{d['incorrect_p10_p50_p90'][1]:>10.3f}"
            f"{f'{d['suggested_band'][0]}–{d['suggested_band'][1]}':>18}  {d['verdict']}"
        )
    print()
    print("Reading: 'correct p50' vs 'bad p50' must be well separated. A verdict of")
    print("'no cutoff' means incorrect and correct scores overlap — that field type")
    print("must always escalate to the LLM judge (the deterministic score alone is")
    print("not decisive). Otherwise the suggested band is the operating cutoff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
