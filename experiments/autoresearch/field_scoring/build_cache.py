#!/usr/bin/env python3
"""Build the labeled-field cache for the field_scoring autoresearch eval.

Reads the pinned Mailroom corpus ground-truth parquet (Mailroom-Corpus-EDA
``data/parquet/ground_truth/train``), keeps non-empty scored fields per doc
class, and writes a small deterministic JSONL to ``.autoresearch_cache/``
(git-ignored: values are real corpus labels, never commit them).

Usage:
    PYTHONPATH=src .venv/bin/python experiments/autoresearch/field_scoring/build_cache.py \
        [--corpus-dir /home/user/Mailroom-Corpus-EDA/data] [--per-class 80] [--seed 7]
"""
from __future__ import annotations

import argparse
import glob
import json
import random
from pathlib import Path

import pandas as pd
import yaml

import llm_dojo_scoring as dojo

REPO = Path(__file__).resolve().parents[3]
CACHE = REPO / ".autoresearch_cache"
SCORED = ("date", "id", "money", "name", "free_text")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus-dir", default="/home/user/Mailroom-Corpus-EDA/data")
    ap.add_argument("--per-class", type=int, default=80)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    dojo.configure_from_taxonomy(yaml.safe_load((REPO / "src/config/taxonomy.yaml").read_text()))
    files = glob.glob(f"{a.corpus_dir}/parquet/ground_truth/train/*.parquet")
    if not files:
        raise SystemExit(f"no ground_truth parquet under {a.corpus_dir}")
    gt = pd.read_parquet(files[0])
    rng = random.Random(a.seed)
    CACHE.mkdir(exist_ok=True)
    out, stats = [], {}
    for cls, grp in gt.groupby("expected"):
        ftypes = dojo.get_field_types(cls)
        rows = list(grp.itertuples())
        rng.shuffle(rows)
        kept = 0
        for r in rows:
            if kept >= a.per_class:
                break
            fields = json.loads(r.gt_fields)
            exp, types = {}, {}
            for f, t in ftypes.items():
                v = fields.get(f)
                if v in (None, "", "[]", "{}", "N/A"):
                    continue
                if t.startswith("entity_list"):
                    try:
                        v = json.loads(v) if isinstance(v, str) else list(v)
                    except Exception:
                        continue
                    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
                        continue
                elif t.split(":")[0] in SCORED:
                    v = str(v)
                else:
                    continue  # label fields are exact-match, not band-scored
                exp[f], types[f] = v, t
            if exp:
                out.append({"doc_class": cls, "filename": r.filename, "types": types, "expected": exp})
                kept += 1
        stats[cls] = kept
    path = CACHE / "field_scoring_labels.jsonl"
    path.write_text("\n".join(json.dumps(o, sort_keys=True) for o in out) + "\n")
    print(f"wrote {len(out)} docs -> {path}  per-class: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
