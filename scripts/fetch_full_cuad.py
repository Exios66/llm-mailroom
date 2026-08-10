#!/usr/bin/env python3
"""Download the FULL CUAD contract dataset for processing, analysis, and EDA.

CUAD (Contract Understanding Atticus Dataset, v1) — 510 real SEC-exhibit
contracts annotated by The Atticus Project (CC BY 4.0). This script fetches:

  - `CUAD_v1.json`          510 annotated contracts (title, full text, and
                             20,910 extractive question-answer annotations over
                             the 41 CUAD clause/attribute types).
  - `full_contract_txt/`    the same contracts as plain text (200 files under
                             Part_I/II/III, organized by agreement category).
  - `full_contract_pdf/`    the original SEC-exhibit PDFs (199 files), the
                             ground truth for PDF ingestion/vision processing.
  - `master_clauses.csv`    the clause-question taxonomy.

Output layout (default `data/cuad/`, gitignored — never commit the corpus):

  data/cuad/
    CUAD_v1.json                      # full annotated dataset
    contracts/                        # one .txt per contract (510)
    contracts_meta.csv                # id, title, part, category, subtype key
    pdfs/                             # the original PDFs (mirror of HF tree)
    master_clauses.csv
    subtype_distribution.json         # subclass distribution + content stats
    EDA.md                            # human-readable EDA report

The subtype taxonomy is the pipeline's own 25 CUAD agreement families
(`langchain_agents/sorter_agent.py:CONTRACT_SUBTYPES` + `_SUBTYPE_ALIASES`):
the HF category folder (e.g. `License_Agreements`, `Supply`, `Franchise`) maps
to the canonical `contract_subtype` key the sorter emits, so the distribution
here is directly comparable with the pipeline's routing output.

Usage:
    python scripts/fetch_full_cuad.py                # download + EDA (all)
    python scripts/fetch_full_cuad.py --skip-download # EDA only (existing data)
    python scripts/fetch_full_cuad.py --no-eda        # download only
    python scripts/fetch_full_cuad.py --data-dir data/cuad
    python scripts/fetch_full_cuad.py --force         # re-download everything
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()

HF_DATASET = "theatticusproject/cuad"
HF_RAW = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main/"

# Files that define the corpus (mirrors the HF repo tree).
ANNOTATIONS_FILE = "CUAD_v1/CUAD_v1.json"
MASTER_CLAUSES_FILE = "CUAD_v1/master_clauses.csv"
TXT_PREFIX = "CUAD_v1/full_contract_txt/"
PDF_PREFIX = "CUAD_v1/full_contract_pdf/"


def _load_subtype_taxonomy():
    """Canonical 25-family taxonomy + folder aliases from the vendored sorter."""
    from langchain_agents.sorter_agent import (
        CONTRACT_SUBTYPES,
        SUBTYPE_UNKNOWN,
        _SUBTYPE_ALIASES,
    )

    labels = {s["key"]: (s["label"], s["description"]) for s in CONTRACT_SUBTYPES}
    return labels, _SUBTYPE_ALIASES, SUBTYPE_UNKNOWN


def _normalize_category(name: str) -> str:
    """'License_Agreements' -> 'license' using the sorter's alias table, or the
    slugified folder name when unmapped."""
    slug = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def subtype_for_title(title: str, labels, aliases, unknown) -> tuple[str, str]:
    """Derive (subtype_key, category) for a CUAD contract from its title or
    folder path (e.g. 'LIMEENERGYCO_...-DISTRIBUTOR AGREEMENT' -> distributor).

    Strategy: first the folder/category aliases (authoritative for the txt/pdf
    tree), then the agreement-family keywords in the title, then 'other'."""
    lower = title.lower()
    words = set(re.findall(r"[a-z0-9]+", lower))

    def has(phrase: str) -> bool:
        return all(w in words for w in re.findall(r"[a-z0-9]+", phrase))

    # 1) folder alias (title may contain the category, e.g. Part_I/License_Agreements/...)
    # Word-bounded so the one-letter alias "ip" cannot match inside "sponsorship".
    for alias, key in aliases.items():
        if has(alias):
            return key, alias
    # 2) agreement-family keyword phrases (word-bounded: "ip" must be its own
    # word, not the tail of "sponsorship"; "supply" not the tail of "supplying")
    family_kw = {
        "affiliate": ("affiliate", "referral"),
        "agency": ("agency agreement",),
        "collaboration": ("collaboration", "cooperation agreement"),
        "co_branding": ("co-branding", "co branding", "cobranding"),
        "consulting": ("consulting", "advisory"),
        "development": ("development agreement", "development and"),
        "distributor": ("distributor", "distribution agreement", "resale"),
        "endorsement": ("endorsement",),
        "franchise": ("franchise", "franchisor", "franchisee"),
        "hosting": ("hosting", "web hosting"),
        "ip": ("intellectual property", "ip agreement"),
        "joint_venture": ("joint venture",),
        "license": ("license", "licensor", "licensee", "licence"),
        "maintenance": ("maintenance",),
        "manufacturing": ("manufacturing", "manufacture"),
        "marketing": ("marketing",),
        "non_compete_no_solicit": ("non-compete", "non compete", "no-solicit", "non-solicit"),
        "outsourcing": ("outsourcing",),
        "promotion": ("promotion",),
        "reseller": ("reseller",),
        "service": ("service agreement", "services agreement", "msa", "master service"),
        "sponsorship": ("sponsorship", "sponsor"),
        "strategic_alliance": ("strategic alliance",),
        "supply": ("supply agreement", "supply of", "purchase"),
        "transportation": ("transportation", "logistics", "carrier", "shipping"),
    }
    for key, kws in family_kw.items():
        if any(has(k) for k in kws):
            return key, "title"
    return unknown, "title"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, chunk: int = 1 << 20) -> Path:
    import shutil
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "mailroom-cuad-fetcher/1.0 (research)"})
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh, chunk)
    return dest


def _list_hf_files() -> list[str]:
    from huggingface_hub import HfApi

    return HfApi().list_repo_files(HF_DATASET, repo_type="dataset")


def download_all(data_dir: Path, force: bool) -> None:
    files = _list_hf_files()
    annotations = [f for f in files if f == ANNOTATIONS_FILE]
    txts = sorted(f for f in files if f.startswith(TXT_PREFIX) and f.endswith(".txt"))
    pdfs = sorted(f for f in files if f.startswith(PDF_PREFIX) and f.endswith(".pdf"))

    print(f"[CUAD] {len(annotations)} annotation file, {len(txts)} txt contracts, {len(pdfs)} PDFs")

    # Annotations (510 contracts, 20,910 QAs).
    _download(HF_RAW + ANNOTATIONS_FILE, data_dir / "CUAD_v1.json", force)

    # master_clauses (taxonomy)
    _download(HF_RAW + MASTER_CLAUSES_FILE, data_dir / "master_clauses.csv", force)

    # Plain-text contracts, flattened into data_dir/contracts/ with a slug name.
    contract_dir = data_dir / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    for f in txts:
        name = Path(f).name
        dest = contract_dir / name
        if dest.exists() and not force:
            continue
        _download(HF_RAW + urllib.parse.quote(f), dest)
        print(f"  txt {name}")

    # PDFs, mirrored by category.
    pdf_dir = data_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for f in pdfs:
        rel = Path(f).relative_to(PDF_PREFIX)
        dest = pdf_dir / rel
        if dest.exists() and not force:
            continue
        _download(HF_RAW + urllib.parse.quote(f), dest)
    print(f"[CUAD] downloads complete: {data_dir}")


# ---------------------------------------------------------------------------
# EDA
# ---------------------------------------------------------------------------

def _contracts_from_annotations(path: Path) -> list[dict]:
    data = json.loads(path.read_text())["data"]
    out = []
    for c in data:
        text = "\n\n".join(p.get("context", "") for p in (c.get("paragraphs") or []))
        qas = [
            {"question": q["question"], "answers": [a["text"] for a in q["answers"]],
             "is_impossible": q.get("is_impossible", False)}
            for p in (c.get("paragraphs") or [])
            for q in (p.get("qas") or [])
        ]
        out.append({"id": c.get("title") or "", "title": c.get("title") or "", "text": text, "qas": qas})
    return out


def _contracts_from_txt(contract_dir: Path) -> list[dict]:
    out = []
    for p in sorted(contract_dir.glob("*.txt")):
        out.append({"id": p.stem, "title": p.stem, "text": p.read_text(errors="replace"), "qas": []})
    return out


def run_eda(data_dir: Path) -> dict:
    """Distribution + content statistics over the corpus. Writes
    subtype_distribution.json + EDA.md into data_dir and returns the summary."""
    labels, aliases, unknown = _load_subtype_taxonomy()

    contracts = []
    ann_path = data_dir / "CUAD_v1.json"
    if ann_path.exists():
        contracts = _contracts_from_annotations(ann_path)
    else:
        contracts = _contracts_from_txt(data_dir / "contracts")
    if not contracts:
        raise SystemExit("No contracts found — run without --skip-download first.")

    # Derive subtype + category per contract (folder alias from title first).
    for c in contracts:
        key, cat = subtype_for_title(c["title"], labels, aliases, unknown)
        c["subtype"] = key
        c["category"] = cat

    dist = Counter(c["subtype"] for c in contracts)
    by_part = Counter()
    for c in contracts:
        title = c["title"]
        for part in ("part_i", "part_ii", "part_iii"):
            if part in title.lower():
                by_part[part] += 1
                break

    content = {
        "total_contracts": len(contracts),
        "total_chars": sum(len(c["text"]) for c in contracts),
        "total_tokens_est": sum(len(c["text"].split()) for c in contracts),
        "median_chars": sorted(len(c["text"]) for c in contracts)[len(contracts) // 2],
        "annotated_qas": sum(len(c["qas"]) for c in contracts),
        "annotated_contracts": sum(1 for c in contracts if c["qas"]),
    }

    # Per-subtype detail: count, mean/median length, sample ids.
    per_subtype: dict[str, dict] = {}
    for key in sorted(dist):
        members = [c for c in contracts if c["subtype"] == key]
        lens = sorted(len(c["text"]) for c in members)
        per_subtype[key] = {
            "count": len(members),
            "pct": round(100 * len(members) / len(contracts), 2),
            "label": labels.get(key, (key, ""))[0],
            "median_chars": lens[len(lens) // 2] if lens else 0,
            "min_chars": lens[0] if lens else 0,
            "max_chars": lens[-1] if lens else 0,
            "sample_ids": [c["title"][:70] for c in members[:3]],
        }

    # Clause/attribute coverage: which of the 41 CUAD question types appear.
    qa_counts: Counter = Counter()
    for c in contracts:
        for q in c["qas"]:
            qa_counts[q["question"][:70]] += 1

    summary = {
        "corpus": HF_DATASET,
        "subtype_taxonomy": "mailroom CONTRACT_SUBTYPES (25 families + other)",
        "distribution": dist,
        "by_part": dict(by_part),
        "content": content,
        "per_subtype": per_subtype,
        "clause_types_present": len(qa_counts),
        "top_clause_types": qa_counts.most_common(12),
    }

    (data_dir / "subtype_distribution.json").write_text(json.dumps(summary, indent=2))
    (data_dir / "EDA.md").write_text(_render_eda_md(summary, labels))
    print(f"[CUAD] EDA written: {data_dir / 'subtype_distribution.json'}, {data_dir / 'EDA.md'}")
    return summary


def _render_eda_md(summary: dict, labels: dict) -> str:
    lines = ["# CUAD Contract Corpus — EDA", ""]
    c = summary["content"]
    lines += [
        f"**Corpus**: {summary['corpus']}",
        f"**Contracts**: {c['total_contracts']} (annotated: {c['annotated_contracts']}, "
        f"QAs: {c['annotated_qas']})",
        f"**Total text**: {c['total_chars']:,} chars, ~{c['total_tokens_est']:,} tokens "
        f"(median {c['median_chars']:,} chars/contract)",
        "",
        "## Contract subclass distribution (mailroom 25-family taxonomy)",
        "",
        "| subtype | label | count | % | median chars |",
        "|---|---|---|---|---|",
    ]
    for key, d in sorted(summary["per_subtype"].items(), key=lambda kv: -kv[1]["count"]):
        lines.append(
            f"| `{key}` | {d['label']} | {d['count']} | {d['pct']}% | {d['median_chars']:,} |"
        )
    lines += ["", "## Clause/attribute coverage (41 CUAD question types)", ""]
    lines += [f"- {q} — {n} annotations" for q, n in summary["top_clause_types"]]
    lines += ["", "## By corpus part", ""]
    for part, n in sorted(summary["by_part"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {part}: {n} contracts")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data" / "cuad",
                        help="Where to store the corpus (default data/cuad, gitignored).")
    parser.add_argument("--skip-download", action="store_true", help="Run EDA only on existing data.")
    parser.add_argument("--no-eda", action="store_true", help="Download only.")
    parser.add_argument("--force", action="store_true", help="Re-download files that already exist.")
    args = parser.parse_args()

    if not args.skip_download:
        download_all(args.data_dir, args.force)
    if not args.no_eda:
        run_eda(args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
