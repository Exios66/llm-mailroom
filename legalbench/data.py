"""Local corpus loaders for the LegalBench suite.

Everything reads from corpora already mirrored under ``data/`` (no network):

- ``contract_qa`` (binary answer): the full CUAD annotation file
  ``data/cuad/CUAD_v1.json`` — 510 contracts x 41 clause categories =
  20,910 yes/no questions with evidence spans. This is the LegalBench
  CUAD / contract-QA binary-answer family.
- ``family_classification`` (multiclass): the 200 plain-text contracts under
  ``data/cuad/contracts/``, each labeled with one of the 25 CUAD contract
  families (+ ``other``) derived from the title via the vendored sorter's
  taxonomy (the same mapping ``fetch_full_cuad`` uses).

Loaders are deterministic given (n, seed) and accept explicit paths so tests
can run against tiny synthetic corpora.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CUAD_JSON = REPO_ROOT / "data" / "cuad" / "CUAD_v1.json"
CUAD_CONTRACTS_DIR = REPO_ROOT / "data" / "cuad" / "contracts"


class CorpusUnavailable(RuntimeError):
    """Raised when a task's local corpus is missing (run the fetch scripts)."""


@dataclass
class Sample:
    """One unit a task is scored on (question or document)."""

    row_id: str
    filename: str
    text: str
    expected: str  # "yes"/"no" for binary; family key for multiclass
    meta: dict[str, Any] = field(default_factory=dict)


def _fingerprint(rows: list[dict[str, Any]]) -> str:
    """Deterministic corpus fingerprint for the sampled rows."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, default=str).encode())
    return digest.hexdigest()


# ------------------------------------------------------------------- CUAD QA


def load_cuad_qa(
    n: int,
    seed: int = 42,
    *,
    cuad_path: Optional[Path] = None,
    min_text_chars: int = 400,
) -> list[dict[str, Any]]:
    """Sample ``n`` (contract, clause-category) pairs from the CUAD annotations.

    Every contract has exactly one QA per clause category (510 x 41 =
    20,910 pairs), so uniform sampling over pairs is deterministic and
    repeatable. The document text is the contract's full text (all paragraph
    contexts concatenated) so the model answers against the whole contract.

    Returns rows: {qa_id, contract_title, category, question, answer
    ("yes"/"no"), evidence, document_text}.
    """
    path = Path(cuad_path or CUAD_JSON)
    if not path.exists():
        raise CorpusUnavailable(
            f"CUAD annotations not found at {path} — run "
            f"`python scripts/fetch_full_cuad.py` first (needs network once)."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data", payload if isinstance(payload, list) else [])
    if not data:
        raise CorpusUnavailable(f"No contracts in {path}")

    by_title: dict[str, dict[str, Any]] = {}
    categories: set[str] = set()
    for contract in data:
        title = str(contract.get("title") or "untitled")
        qas: dict[str, dict[str, Any]] = {}
        for para in contract.get("paragraphs") or []:
            ctx = str(para.get("context") or "")
            for qa in para.get("qas") or []:
                qid = str(qa.get("id") or "")
                # The CUAD id is "<TITLE>__<Clause Category>" (the category is
                # the last segment). Exactly one QA per (contract, category).
                if "__" in qid:
                    cat = qid.split("__", 1)[1].strip()
                else:
                    cat = "unknown"
                categories.add(cat)
                qas.setdefault(cat, {"qa": qa, "ctx": ctx, "qid": qid})
        if qas:
            by_title[title] = {"contract": contract, "qas": qas}

    titles = sorted(by_title)
    if not titles:
        raise CorpusUnavailable(f"No QA pairs in {path}")

    # Universe of ACTUAL (title, category) pairs, sorted deterministically.
    # (In the real corpus every contract carries all 41 categories, so this
    # is exactly 510 x 41 = 20,910; contracts with partial coverage are
    # handled correctly too.)
    pairs = [
        (title, cat)
        for title in titles
        for cat in sorted(by_title[title]["qas"])
    ]
    k = min(n, len(pairs))
    idx = random.Random(seed).sample(range(len(pairs)), k)

    text_cache: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for i in idx:
        title, cat = pairs[i]
        entry = by_title[title]["qas"][cat]
        qid = entry["qid"]
        qa = entry["qa"]
        text = text_cache.get(title)
        if text is None:
            text = "\n\n".join(
                str(p.get("context") or "")
                for p in by_title[title]["contract"].get("paragraphs") or []
            )
            text_cache[title] = text
        if len(text) < min_text_chars:
            continue
        answers = qa.get("answers") or []
        impossible = bool(qa.get("is_impossible"))
        evidence = str(answers[0].get("text") or "") if answers else ""
        rows.append(
            {
                "qa_id": qid,
                "contract_title": title,
                "category": cat,
                "question": str(qa.get("question") or ""),
                "answer": "no" if (impossible or not answers) else "yes",
                "evidence": evidence,
                "document_text": text,
            }
        )
    return rows


# ------------------------------------------------------ family classification


def load_family_rows(
    n: int,
    seed: int = 42,
    *,
    contracts_dir: Optional[Path] = None,
    min_text_chars: int = 400,
) -> list[dict[str, Any]]:
    """Sample ``n`` CUAD contract texts with their 25-family labels.

    Labels come from the vendored sorter's taxonomy applied to the contract
    title (the authoritative mapping used by ``fetch_full_cuad`` and the
    subtype eval loop). Rows: {doc_id, title, text, family, family_label}.
    """
    from scripts.fetch_full_cuad import _load_subtype_taxonomy, subtype_for_title

    labels, aliases, unknown = _load_subtype_taxonomy()
    directory = Path(contracts_dir or CUAD_CONTRACTS_DIR)
    if not directory.is_dir():
        raise CorpusUnavailable(
            f"CUAD contract texts not found at {directory} — run "
            f"`python scripts/fetch_full_cuad.py` first (needs network once)."
        )
    files = sorted(directory.glob("*.txt"))
    if not files:
        raise CorpusUnavailable(f"No .txt contracts in {directory}")

    candidates: list[dict[str, Any]] = []
    for f in files:
        title = f.stem
        text = f.read_text(encoding="utf-8", errors="replace")
        if len(text) < min_text_chars:
            continue
        family, _category = subtype_for_title(title, labels, aliases, unknown)
        candidates.append(
            {
                "doc_id": title,
                "title": title,
                "text": text,
                "family": family,
                "family_label": labels.get(family, (family, ""))[0],
            }
        )
    if not candidates:
        raise CorpusUnavailable(f"No labeled contracts in {directory}")

    picked = random.Random(seed).sample(candidates, min(n, len(candidates)))
    return picked


def _normalize_prediction(value: Any) -> Optional[str]:
    """'yes'/'no' normalization for binary answers (lenient)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    first = s.split()[0].strip(".,;:!?")
    if first in ("yes", "y"):
        return "yes"
    if first in ("no", "n"):
        return "no"
    return None
