"""Build scorable extraction GT for every live specialist class.

Hub / catalog labels win. Post-hoc regexes fill remaining schema fields so
model outputs can be compared even when the published merge is subclass-only
(corporate S-1s) or column-sparse (Enron mail). Provenance is recorded so a
post-hoc fill is never billed as an official Hub annotation.
"""

from __future__ import annotations

from typing import Any

from langchain_agents.cuad_maud import (
    flatten_cuad_clause_labels,
    flatten_maud_clause_labels,
    normalize_consideration,
)
from langchain_agents.doc_inventories import (
    COMPLIANCE_GT_KEYS,
    CORPORATE_GT_KEYS,
    CORRESPONDENCE_GT_KEYS,
    INSURANCE_GT_KEYS,
    coerce_gt_value,
    normalize_claim_type,
    normalize_communication_type,
    normalize_filing_type,
    normalize_record_type,
)
from observability.posthoc_gt import extract_posthoc_fields
from observability.specialist_suites import (
    gt_schema_coverage,
    specialist_for_class,
)

CONTRACT_GT_KEYS: tuple[str, ...] = (
    "document_name",
    "parties",
    "effective_date",
    "term_length",
    "governing_law",
    "contract_value",
    "renewal_terms",
    "cuad_family",
    "merger_consideration",
    "cuad_clauses",
    "maud_clauses",
)


def _put(dst: dict[str, Any], key: str, value: Any) -> None:
    coerced = coerce_gt_value(value)
    if coerced in (None, "", [], {}):
        return
    dst[key] = coerced


def catalog_expected_fields(sample: dict) -> dict[str, Any]:
    """Labels that already exist on the sample / Hub row (no text parsing)."""
    expected_fields: dict[str, Any] = {}
    if sample.get("cuad_clauses"):
        expected_fields["cuad_clauses"] = list(sample["cuad_clauses"])
    elif sample.get("cuad_clause_labels"):
        expected_fields["cuad_clauses"] = flatten_cuad_clause_labels(
            sample["cuad_clause_labels"]
        )
    if sample.get("maud_clauses"):
        expected_fields["maud_clauses"] = list(sample["maud_clauses"])
    elif sample.get("maud_clause_labels"):
        expected_fields["maud_clauses"] = flatten_maud_clause_labels(
            sample["maud_clause_labels"]
        )
    existing = sample.get("expected_fields")
    if isinstance(existing, dict):
        for key, value in existing.items():
            _put(expected_fields, key, value)
    hf_class = sample.get("expected_hf_class") or sample.get("expected") or ""
    subclass = sample.get("expected_subclass") or ""
    if hf_class == "contract" and subclass:
        from langchain_agents.sorter_agent import normalize_subtype

        expected_fields["cuad_family"] = normalize_subtype(subclass)
    if hf_class == "merger_agreement" and subclass:
        token = normalize_consideration(subclass)
        if token:
            expected_fields["merger_consideration"] = token
    if hf_class == "corporate_record":
        if subclass:
            token = normalize_record_type(subclass)
            expected_fields["record_type"] = token or subclass
        # Legacy Hub column → semantic enrichment
        legacy_provisions = sample.get("key_provisions")
        if legacy_provisions and not expected_fields.get("subject_matter"):
            coerced = coerce_gt_value(legacy_provisions)
            if isinstance(coerced, list) and coerced:
                _put(expected_fields, "subject_matter", str(coerced[0])[:240])
                _put(
                    expected_fields,
                    "keywords",
                    [" ".join(str(p).split()[:4]) for p in coerced[:8]],
                )
                _put(expected_fields, "intent", "record_governance")
        for key in CORPORATE_GT_KEYS:
            if key == "record_type" and expected_fields.get("record_type"):
                continue
            val = sample.get(key)
            if val not in (None, ""):
                _put(expected_fields, key, val)
    if hf_class == "correspondence":
        if subclass:
            token = normalize_communication_type(subclass)
            expected_fields["communication_type"] = token or subclass
        legacy_points = sample.get("key_points")
        if legacy_points and not expected_fields.get("subject_matter"):
            coerced = coerce_gt_value(legacy_points)
            if isinstance(coerced, list) and coerced:
                _put(expected_fields, "subject_matter", str(coerced[0])[:240])
                _put(
                    expected_fields,
                    "keywords",
                    [" ".join(str(p).split()[:4]) for p in coerced[:8]],
                )
                _put(expected_fields, "intent", "correspondence")
        for key in CORRESPONDENCE_GT_KEYS:
            if key == "communication_type" and expected_fields.get("communication_type"):
                continue
            val = sample.get(key)
            if val not in (None, ""):
                _put(expected_fields, key, val)
    if hf_class == "compliance_filing":
        if subclass:
            token = normalize_filing_type(subclass)
            expected_fields["filing_type"] = token or subclass
        for key in COMPLIANCE_GT_KEYS:
            if key == "filing_type" and expected_fields.get("filing_type"):
                continue
            val = sample.get(key)
            if val not in (None, ""):
                _put(expected_fields, key, val)
    if hf_class == "insurance_claim":
        claim = sample.get("claim_type") or subclass
        token = normalize_claim_type(claim)
        if token or claim:
            expected_fields["claim_type"] = token or claim
        for key in INSURANCE_GT_KEYS:
            if key == "claim_type":
                continue
            val = sample.get(key)
            if val not in (None, ""):
                _put(expected_fields, key, val)
    if hf_class in ("contract", "merger_agreement"):
        for key in CONTRACT_GT_KEYS:
            if expected_fields.get(key) not in (None, "", [], {}):
                continue
            val = sample.get(key)
            if val not in (None, ""):
                _put(expected_fields, key, val)
    for key in ("content_topic", "sentiment_label", "maud_clause_labels"):
        val = sample.get(key)
        if val not in (None, ""):
            _put(expected_fields, key, val)
    return expected_fields


def build_expected_fields(sample: dict) -> tuple[dict[str, Any], dict[str, Any]]:
    """Catalog labels + post-hoc fills. Hub / explicit values are never overwritten."""
    fields = catalog_expected_fields(sample)
    hub_keys = set(fields)
    hf_class = str(sample.get("expected_hf_class") or sample.get("expected") or "")
    text = sample.get("text") or sample.get("doc_text") or ""
    posthoc = extract_posthoc_fields(hf_class, text)
    sources = {key: "hub" for key in fields}
    n_posthoc = 0
    for key, value in posthoc.items():
        if fields.get(key) not in (None, "", [], {}):
            continue
        if value in (None, "", [], {}):
            continue
        fields[key] = value
        sources[key] = "posthoc"
        n_posthoc += 1
    coverage = gt_schema_coverage(hf_class, fields)
    meta = {
        "n_fields": len(fields),
        "n_hub": len(hub_keys),
        "n_posthoc": n_posthoc,
        "sources": sources,
        "specialist": specialist_for_class(hf_class),
        "doc_class": hf_class,
        **coverage,
    }
    return fields, meta


def presence_expectations_from_ground_truth(
    gt: dict[str, Any] | None,
    doc_class: str | None = None,
) -> dict[str, dict] | None:
    """Build CUAD ``presence_expectations`` for ``extraction_category_presence``.

    Returns ``None`` when there is nothing to score — callers must omit the
    score rather than emit 0.0. Only ``contract`` / ``merger_agreement`` have
    CUAD presence categories. Prefers an already-shaped
    ``gt["presence_expectations"]``, then Hub ``cuad_clause_labels`` (empty
    list = expected False; non-empty = True + first span), then flattened
    ``expected_fields.cuad_clauses`` / ``gt["cuad_clauses"]`` lines.
    """
    if not isinstance(gt, dict):
        return None
    cls = str(
        doc_class
        or gt.get("expected_doc_class")
        or gt.get("expected_hf_class")
        or gt.get("expected")
        or ""
    )
    if cls not in ("contract", "merger_agreement"):
        return None
    explicit = gt.get("presence_expectations")
    if isinstance(explicit, dict) and explicit:
        return explicit

    expected_fields = gt.get("expected_fields")
    if not isinstance(expected_fields, dict):
        expected_fields = {}

    labels = gt.get("cuad_clause_labels")
    if labels in (None, ""):
        labels = expected_fields.get("cuad_clause_labels")
    if labels not in (None, ""):
        built = _presence_from_cuad_label_map(labels)
        if built:
            return built

    lines = expected_fields.get("cuad_clauses") or gt.get("cuad_clauses")
    if lines:
        return _presence_from_cuad_lines(lines)
    return None


def _presence_entry(expected: bool, answer: str = "") -> dict:
    return {"expected": bool(expected), "answer": answer or "", "field": "cuad_clauses"}


def _presence_from_cuad_label_map(raw: Any) -> dict[str, dict] | None:
    from langchain_agents.cuad_maud import CUAD_CLAUSE_CATEGORIES, parse_json_obj

    parsed = raw if isinstance(raw, dict) else parse_json_obj(raw)
    if not isinstance(parsed, dict) or not parsed:
        return None
    folded = {str(key).strip().casefold(): value for key, value in parsed.items()}
    out: dict[str, dict] = {}
    for cat in CUAD_CLAUSE_CATEGORIES:
        spans = folded.get(cat.casefold())
        texts: list[str] = []
        items = spans if isinstance(spans, list) else ([] if spans in (None, "") else [spans])
        for span in items:
            if span in (None, "", [], {}):
                continue
            if isinstance(span, dict):
                text = str(span.get("text") or "").strip()
            else:
                text = str(span).strip()
            if text:
                texts.append(text)
        out[cat] = _presence_entry(bool(texts), texts[0] if texts else "")
    return out


def _presence_from_cuad_lines(lines: Any) -> dict[str, dict] | None:
    from langchain_agents.cuad_maud import CUAD_CLAUSE_CATEGORIES

    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list) or not lines:
        return None
    canon = {cat.casefold(): cat for cat in CUAD_CLAUSE_CATEGORIES}
    present: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, str) or ":" not in line:
            continue
        cat, _, rest = line.partition(":")
        key = canon.get(cat.strip().casefold())
        if not key or key in present:
            continue
        present[key] = rest.strip()
    if not present:
        return None
    return {
        cat: _presence_entry(cat in present, present.get(cat, ""))
        for cat in CUAD_CLAUSE_CATEGORIES
    }
