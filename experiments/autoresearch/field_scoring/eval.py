#!/usr/bin/env python3
"""Autoresearch eval: field_scoring bands in src/config/taxonomy.yaml.

Frozen. The agent edits ONLY the ``field_scoring:`` block of
``src/config/taxonomy.yaml``. This script loads that file, wires it into the
pinned Dojo scorer (``configure_from_taxonomy`` -- the same path the pipeline
uses), scores labeled (prediction, ground-truth) pairs per field and asks
what the bands would do with each one:

  * not ambiguous and score >= band high  -> auto-ACCEPT
  * not ambiguous and score <  band low   -> auto-REJECT
  * ambiguous (inside the band)           -> ESCALATE to the LLM judge (costs a call)

Labels are correct/incorrect BY CONSTRUCTION from real corpus ground truth
(Mailroom-Corpus-EDA v9.3), as in src/scripts/calibrate_field_scoring.py.
Two perturbation families are used:
  dev      calibrate_field_scoring._predictions_for (what the repo already uses)
  heldout  a different family written for this eval (hard near-misses and
           cross-document swaps); never used for SCORE, only for the guard.

SCORE  = macro-average over field types of (auto-decided AND right) / n, on dev.
GUARD  = wrong auto-decision rate may not rise above the recorded baseline.json by more than
         0.005 for any field type, or 0.003 overall, on dev OR heldout. (Some wrong decisions are
         band-independent -- the Dojo scorer itself returns 1.0 for e.g. a truncated person name --
         so the guard is relative to the baseline, not an absolute cap.)
Prints ``SCORE: x.xxxx`` and ``GUARD_PASS: True|False``. ``--write-baseline`` records baseline.json. No network, no LLM
(sentence-transformers weights are fetched once on first use and cached).
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
CACHE = REPO / ".autoresearch_cache" / "field_scoring_labels.jsonl"
TAXONOMY = Path(os.environ.get("AUTORESEARCH_TAXONOMY", REPO / "src/config/taxonomy.yaml"))
BASELINE = Path(__file__).with_name("baseline.json")
GUARD_TYPE_DELTA, GUARD_OVERALL_DELTA = 0.005, 0.003  # allowed rise in wrong-auto-decision rate vs baseline
# Frozen copy (NOT read from the tunable block): list fields whose ground truth is a partial
# sample, so a prediction that is a superset of the labels is still correct (recall semantics).
PARTIAL_GT = {"parties", "keywords", "cuad_clauses", "maud_clauses", "claim_checklist", "action_items"}

os.environ.pop("LLM_DOJO_SCORING_CONFIG", None)  # would override taxonomy.yaml wholesale
sys.path.insert(0, str(REPO / "src"))
import llm_dojo_scoring as dojo  # noqa: E402


def _load_dev_generator():
    spec = importlib.util.spec_from_file_location("calib", REPO / "src/scripts/calibrate_field_scoring.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._predictions_for


# ---------------------------------------------------------------- heldout family
def _transpose(s: str, rng: random.Random) -> str:
    idx = [i for i in range(len(s) - 1) if s[i] != s[i + 1] and s[i].isalnum() and s[i + 1].isalnum()]
    if not idx:
        return s + "0"
    i = rng.choice(idx)
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]


def _heldout(ftype: str, exp, pool: list, rng: random.Random):
    """Yield (prediction, is_correct). ``pool`` = other docs' values, same field."""
    base = ftype.split(":")[0]
    others = [p for p in pool if p != exp]
    other = rng.choice(others) if others else None
    if base == "entity_list":
        if not exp:
            return
        shuf = list(exp); rng.shuffle(shuf)
        yield shuf, True
        yield [x.upper() for x in exp], True
        yield exp[:1] if len(exp) > 1 else [exp[0] + " Holdings"], False
        if other:
            yield other, False
            half = exp[: max(1, len(exp) // 2)] + (other[:1] if other else [])
            yield half, False
        return
    text = str(exp)
    if base == "money":
        nums = re.findall(r"\d[\d,]*\.?\d*", text)
        if not nums:
            return
        raw = nums[0].replace(",", "")
        try:
            n = float(raw)
        except ValueError:
            return
        yield f"${n:,.2f}", True
        yield f"{n:.0f}" if n == int(n) else f"{n:.2f}", True
        yield f"{_transpose(raw.split('.')[0], rng)}", False
        yield f"{n * 1.05:.2f}", False
        yield f"{n * 0.95:.2f}", False
    elif base == "date":
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
        if not m:
            return
        y, mo, d = m.groups()
        import calendar
        yield f"{calendar.month_name[int(mo)]} {int(d)}, {y}", True
        yield f"{mo}/{d}/{y}", True
        if int(d) <= 12 and d != mo:
            yield f"{y}-{d}-{mo}", False
        yield f"{y}-{mo}-{min(28, int(d) + 7):02d}" if int(d) < 21 else f"{y}-{mo}-{max(1, int(d) - 7):02d}", False
    elif base == "id":
        yield text.lower(), True
        yield re.sub(r"[\s]+", "", text), True
        yield _transpose(text, rng), False
        i = rng.randrange(len(text)) if text else 0
        yield text[:i] + ("7" if text[i:i + 1] != "7" else "3") + text[i + 1:], False
        if other:
            yield str(other), False
    elif base == "name":
        yield text.upper(), True
        yield text.rstrip(".,"), True
        parts = text.split()
        if len(parts) >= 2:
            yield " ".join(parts[:-1]), False
        if other:
            yield str(other), False
        yield _transpose(text, rng), False
    else:  # free_text
        yield re.sub(r"\s+", " ", text.strip()).rstrip("."), True
        yield text.upper(), True
        words = text.split()
        if len(words) >= 6:
            keep = [w for i, w in enumerate(words) if i % 5 != 4]  # drop 20% of tokens
            yield " ".join(keep), True
            half = words[: len(words) // 2]
            yield " ".join(half), False
        if other:
            yield str(other), False
            ow = str(other).split()
            mixed = words[: len(words) // 2] + ow[len(ow) // 2:]
            yield " ".join(mixed), False


def _bucket(ftype: str) -> str:
    return "entity_list" if ftype.startswith("entity_list") else ftype


def _decide(bands, ftype, score, ambiguous):
    if ambiguous:
        return "escalate"
    band = bands.get(_bucket(ftype))
    if not band or band[0] == "never":  # exact-after-normalize types: only a perfect score is accepted
        return "accept" if score >= 0.999 else "reject"
    return "accept" if score >= band[1] else "reject"


def main() -> int:
    if not CACHE.exists():
        print(f"missing {CACHE}: run build_cache.py first", file=sys.stderr)
        return 2
    dojo.configure_from_taxonomy(yaml.safe_load(TAXONOMY.read_text()))
    bands = dojo.get_type_bands()
    docs = [json.loads(l) for l in CACHE.read_text().splitlines() if l.strip()]
    pools: dict[tuple, list] = defaultdict(list)
    for d in docs:
        for f, v in d["expected"].items():
            pools[(d["doc_class"], f)].append(v)
    dev_gen = _load_dev_generator()

    stats = {fam: defaultdict(lambda: defaultdict(int)) for fam in ("dev", "heldout")}
    for i, d in enumerate(docs):
        cls = d["doc_class"]
        for f, ftype in d["types"].items():
            exp = d["expected"][f]
            for fam in ("dev", "heldout"):
                rng = random.Random(f"{fam}:{i}:{f}")
                gen = dev_gen(ftype, exp, rng) if fam == "dev" else _heldout(ftype, exp, pools[(cls, f)], rng)
                for pred, correct in gen:
                    if pred is None or pred == "" or pred == []:
                        continue
                    if (not correct and f in PARTIAL_GT and isinstance(pred, list) and isinstance(exp, list)
                            and {str(x).lower() for x in exp} <= {str(x).lower() for x in pred}):
                        correct = True  # superset of a partial-GT list is correct, not an error
                    res = dojo.score_extraction(cls, {f: ftype}, {f: pred}, {f: exp})
                    score = res.field_scores.get(f) or 0.0
                    dec = _decide(bands, ftype, score, f in (res.ambiguous_fields or []))
                    s = stats[fam][_bucket(ftype)]
                    s["n"] += 1
                    if dec == "escalate":
                        s["escalated"] += 1
                    elif (dec == "accept") == correct:
                        s["auto_right"] += 1
                    else:
                        s["auto_wrong"] += 1

    def summarize(fam):
        per, tn, tw = {}, 0, 0
        for t, s in sorted(stats[fam].items()):
            n = s["n"]
            per[t] = {"n": n, "auto_right": s["auto_right"] / n, "auto_wrong": s["auto_wrong"] / n,
                      "escalated": s["escalated"] / n}
            tn += n
            tw += s["auto_wrong"]
        macro = sum(p["auto_right"] for p in per.values()) / max(1, len(per))
        return per, macro, (tw / tn if tn else 0.0)

    dev_per, dev_macro, dev_wrong = summarize("dev")
    ho_per, ho_macro, ho_wrong = summarize("heldout")
    current = {"dev": {t: p["auto_wrong"] for t, p in dev_per.items()} | {"_overall": dev_wrong},
               "heldout": {t: p["auto_wrong"] for t, p in ho_per.items()} | {"_overall": ho_wrong}}
    if "--write-baseline" in sys.argv:
        BASELINE.write_text(json.dumps({"wrong_rates": current, "dev_macro": dev_macro}, indent=2) + "\n")
        print(f"wrote {BASELINE}")
    base = json.loads(BASELINE.read_text())["wrong_rates"] if BASELINE.exists() else None
    violations = []
    if base:
        for fam in ("dev", "heldout"):
            for t, v in current[fam].items():
                lim = GUARD_OVERALL_DELTA if t == "_overall" else GUARD_TYPE_DELTA
                if v > base[fam].get(t, 0.0) + lim:
                    violations.append(f"{fam}/{t}: wrong {v:.4f} > baseline {base[fam].get(t, 0.0):.4f} + {lim}")
    guard = not violations

    print(f"{'type':<14}{'n(dev)':>7}{'right':>8}{'wrong':>8}{'esc':>8} | {'n(ho)':>6}{'right':>8}{'wrong':>8}{'esc':>8}")
    for t in dev_per:
        a, b = dev_per[t], ho_per.get(t, {"n": 0, "auto_right": 0, "auto_wrong": 0, "escalated": 0})
        print(f"{t:<14}{a['n']:>7}{a['auto_right']:>8.3f}{a['auto_wrong']:>8.3f}{a['escalated']:>8.3f} | "
              f"{b['n']:>6}{b['auto_right']:>8.3f}{b['auto_wrong']:>8.3f}{b['escalated']:>8.3f}")
    print(f"DEV_WRONG_RATE: {dev_wrong:.4f}")
    print(f"HELDOUT_WRONG_RATE: {ho_wrong:.4f}")
    print(f"HELDOUT_MACRO_AUTO_RIGHT: {ho_macro:.4f}  (informational)")
    for v in violations:
        print(f"GUARD_VIOLATION: {v}")
    if base is None:
        print("NOTE: no baseline.json yet (run with --write-baseline on the untouched config)")
    print(f"GUARD_PASS: {guard}")
    print(f"SCORE: {dev_macro:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
