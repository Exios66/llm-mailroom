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
    "termination_clauses",
    "governing_law",
    "key_obligations",
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
