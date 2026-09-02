"""Lucius-Morningstar Hugging Face corpora the mailroom pipeline can ingest.

``Lucius-Morningstar/mailroom-corpus`` schema **v7** is the targeted full
corpus (1,650 documents: CUAD contracts, MAUD merger agreements, S-1
corporate records, Enron correspondence sample, CMS insurance claims).
v7 adds correspondence intent hydration (issue #5): every correspondence row
carries a canonical 8-class intent plus `intent_source` / `intent_confidence`
/ `intent_status` provenance on the `ground_truth` config.

Class × subclass examples come from ``docclass-pilot`` (a deterministic
stratified slice of that parent — every type and every subtype stratum).
Other published Lucius-Morningstar datasets are first-class pipeline inputs
too, including the 247k-row Enron correspondence corpus.

``compliance_filing`` is retired from the canonical five-class surface (zero
Hub rows; retained in ``taxonomy.yaml`` as ``status: retired`` machinery).
Court/DD are retired.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ORG = "Lucius-Morningstar"
FULL_CORPUS_SCHEMA = "v7"
# Renamed 2026-09-02 per human directive: the Hub repo was `docclass-merged`
# ("docclass" was always a placeholder) — now `mailroom-corpus`. The Hub
# serves a redirect from the old id, and the internal corpus SLUG below stays
# `docclass-merged` (historical traces carry the immutable
# `source-docclass-merged` tag; slug/aliases are plumbing, not identity).
FULL_CORPUS_ID = f"{ORG}/mailroom-corpus"
# v7 tip bb57c5ad (issue #5 fix: intent_source aeslc_join on the 162
# join-assisted rows; supersedes 1acd2600 hydration + fc1f211c card bump).
# Pinned per the corpus plan §44 — never evaluate against unpinned main.
# Pin survives the rename (move_repo preserves git history).
FULL_CORPUS_REVISION = "bb57c5ad00333d239ea456fe3f2298c3ba5b5108"
EXAMPLES_ID = f"{ORG}/docclass-pilot"

# Hub HF classes present in mailroom-corpus (v7) — identical to the canonical
# five-class live taxonomy (docs/v7-taxonomy.md). taxonomy.yaml carries a
# sixth configured entry (compliance_filing, marked status: retired):
# retained machinery with zero Hub rows, not a corpus class.
HUB_CLASSES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES_JSON = (
    _REPO / "notebooks" / "fixtures" / "huggingface" / "class_subclass_examples.json"
)

CORPORA: dict[str, dict[str, Any]] = {
    "docclass-merged": {
        "slug": "docclass-merged",
        "id": FULL_CORPUS_ID,
        "revision": FULL_CORPUS_REVISION,
        "schema": FULL_CORPUS_SCHEMA,
        "role": "full_corpus",
        "pipeline": True,
        "n_docs": 1650,
        "classes": HUB_CLASSES,
        "gt_config": "ground_truth",
        "row_shape": "docclass",
        "text_field": "doc_text",
        "source_tag": "source-docclass-merged",
    },
    "docclass-pilot": {
        "slug": "docclass-pilot",
        "id": EXAMPLES_ID,
        "revision": None,
        "schema": FULL_CORPUS_SCHEMA,
        "role": "class_subclass_examples",
        "pipeline": True,
        "n_docs": 138,
        "n_strata": 48,
        "classes": HUB_CLASSES,
        "gt_config": "ground_truth",
        "row_shape": "docclass",
        "text_field": "doc_text",
        "source_tag": "source-docclass-pilot",
    },
    "enron-correspondence-dedup": {
        "slug": "enron-correspondence-dedup",
        "id": f"{ORG}/enron-correspondence-dedup",
        "revision": None,
        "schema": None,
        "role": "correspondence_full",
        "pipeline": True,
        "n_docs": 247523,
        "classes": ("correspondence",),
        "default_class": "correspondence",
        "gt_config": "ground_truth",
        "row_shape": "enron",
        "text_field": "text",
        "source_tag": "source-enron-correspondence",
    },
    "cms-desynpuf-insurance-claims": {
        "slug": "cms-desynpuf-insurance-claims",
        "id": f"{ORG}/cms-desynpuf-insurance-claims",
        "revision": None,
        "schema": None,
        "role": "insurance_claims",
        "pipeline": True,
        "n_docs": 400,
        "classes": ("insurance_claim",),
        "default_class": "insurance_claim",
        "gt_config": None,
        "row_shape": "cms_inline",
        "text_field": "doc_text",
        "source_tag": "source-cms-desynpuf",
    },
    "mailroom-cuad-contracts-full": {
        "slug": "mailroom-cuad-contracts-full",
        "id": f"{ORG}/mailroom-cuad-contracts-full",
        "revision": None,
        "schema": None,
        "role": "cuad_contracts",
        "pipeline": True,
        "n_docs": 510,
        "classes": ("contract",),
        "default_class": "contract",
        "gt_config": None,
        "row_shape": "braintrust_mirror",
        "text_field": "input",
        "source_tag": "source-cuad-full",
    },
    "mailroom-cuad-contracts": {
        "slug": "mailroom-cuad-contracts",
        "id": f"{ORG}/mailroom-cuad-contracts",
        "revision": None,
        "schema": None,
        "role": "cuad_contracts_sample",
        "pipeline": True,
        "n_docs": 50,
        "classes": ("contract",),
        "default_class": "contract",
        "gt_config": None,
        "row_shape": "braintrust_mirror",
        "text_field": "input",
        "source_tag": "source-cuad-sample",
    },
    "legalbench-full": {
        "slug": "legalbench-full",
        "id": f"{ORG}/legalbench-full",
        "revision": None,
        "schema": None,
        "role": "legalbench_tasks",
        "pipeline": False,
        "n_docs": None,
        "classes": (),
        "gt_config": None,
        "row_shape": "legalbench_tasks",
        "text_field": None,
        "source_tag": "source-legalbench-full",
        "note": "LegalBench task pack (contract_qa / family_classification), not a "
        "document-pipeline ingest corpus. Run via python -m legalbench.cli.",
    },
}

_ALIASES = {
    "v5": "docclass-merged",
    "v7": "docclass-merged",
    "full": "docclass-merged",
    "merged": "docclass-merged",
    # renamed 2026-09-02: Hub repo mailroom-corpus (formerly docclass-merged)
    "corpus": "docclass-merged",
    "mailroom-corpus": "docclass-merged",
    "examples": "docclass-pilot",
    "pilot": "docclass-pilot",
    "enron": "enron-correspondence-dedup",
    "correspondence": "enron-correspondence-dedup",
    "claims": "cms-desynpuf-insurance-claims",
    "insurance": "cms-desynpuf-insurance-claims",
    "cuad": "mailroom-cuad-contracts-full",
    "cuad-full": "mailroom-cuad-contracts-full",
}

_ACTIVE_SLUG = "docclass-merged"


def pipeline_corpora() -> list[dict[str, Any]]:
    return [c for c in CORPORA.values() if c.get("pipeline")]


def resolve_corpus(name: str | None) -> dict[str, Any]:
    """Resolve a slug, alias, or ``Lucius-Morningstar/<slug>`` id."""
    raw = (name or "docclass-merged").strip()
    if raw.startswith(f"{ORG}/"):
        raw = raw.split("/", 1)[1]
    slug = _ALIASES.get(raw, raw)
    if slug not in CORPORA:
        known = ", ".join(sorted(CORPORA))
        raise KeyError(f"unknown Hugging Face corpus {name!r}; known: {known}")
    return CORPORA[slug]


def set_active_corpus(name: str | None) -> dict[str, Any]:
    global _ACTIVE_SLUG
    corp = resolve_corpus(name)
    _ACTIVE_SLUG = corp["slug"]
    return corp


def active_corpus() -> dict[str, Any]:
    return CORPORA[_ACTIVE_SLUG]


def adapt_hub_row(row: dict[str, Any], corpus: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a Hub row into the mailroom-corpus shape parse_hf_row expects."""
    corp = corpus or active_corpus()
    shape = corp.get("row_shape") or "docclass"
    data = dict(row or {})
    if shape == "braintrust_mirror":
        text = ""
        raw_in = data.get("input") or data.get("doc_text") or ""
        if isinstance(raw_in, str) and raw_in.lstrip().startswith("{"):
            try:
                parsed = json.loads(raw_in)
                if isinstance(parsed, dict):
                    text = str(parsed.get("doc_text") or "")
            except json.JSONDecodeError:
                text = raw_in
        elif isinstance(raw_in, str):
            text = raw_in
        meta = data.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        family = (
            meta.get("expected_subclass")
            or meta.get("category")
            or (meta.get("applicable_categories") or [None])[0]
            or ""
        )
        if isinstance(family, list):
            family = family[0] if family else ""
        data["filename"] = str(data.get("filename") or data.get("id") or "cuad.txt")
        data["doc_text"] = text
        data["expected"] = data.get("expected") if str(data.get("expected") or "") in HUB_CLASSES else "contract"
        data["expected_subclass"] = str(family or "")
        return data
    if shape == "enron":
        data.setdefault("doc_text", data.get("text") or "")
        if not data.get("expected"):
            data["expected"] = corp.get("default_class") or "correspondence"
        return data
    if shape == "cms_inline":
        data.setdefault("expected", "insurance_claim")
        return data
    field = corp.get("text_field") or "doc_text"
    if field != "doc_text" and not data.get("doc_text"):
        data["doc_text"] = data.get(field) or ""
    if not data.get("expected") and corp.get("default_class"):
        data["expected"] = corp["default_class"]
    return data


@lru_cache(maxsize=1)
def load_example_pack() -> dict[str, Any]:
    """Committed Dataset Viewer snapshot of docclass-pilot class×subclass rows."""
    if not _EXAMPLES_JSON.exists():
        raise FileNotFoundError(
            f"missing Hub example pack {_EXAMPLES_JSON} — class/subtype "
            "examples must come from Lucius-Morningstar/docclass-pilot"
        )
    return json.loads(_EXAMPLES_JSON.read_text(encoding="utf-8"))


def example_rows() -> list[dict[str, Any]]:
    pack = load_example_pack()
    return list(pack.get("examples") or [])


def example_for_class(doc_class: str, *, subclass: str | None = None) -> dict[str, Any]:
    """One Hub example for a class (optionally a specific subclass)."""
    rows = example_rows()
    if subclass:
        want = str(subclass).strip()
        hits = [
            r for r in rows
            if r.get("expected") == doc_class and str(r.get("expected_subclass") or "") == want
        ]
        if hits:
            return hits[0]
    for row in rows:
        if row.get("expected") == doc_class:
            return row
    raise KeyError(f"no Hub example for class {doc_class!r} subclass={subclass!r}")


def examples_by_class() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in example_rows():
        cls = str(row.get("expected") or "")
        if cls and cls not in out:
            out[cls] = row
    return out


def hub_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a pack row into the HF-pilot sample dict."""
    text = str(row.get("doc_text") or "")
    return {
        "filename": str(row.get("filename") or "doc.txt"),
        "text": text,
        "expected_hf_class": str(row.get("expected") or ""),
        "expected_subclass": str(row.get("expected_subclass") or ""),
        "chars": int(row.get("chars") or len(text)),
    }
