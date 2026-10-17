"""llm-dojo-scoring enhanced scoring wired into mailroom (pin >= 0.19).

``score_with_suite`` / the HF pilot route through ``suite.score_document``
(full per-document payload, fail-closed unscorable GT, provenance), and the
dojo archive hash stays byte-identical to mailroom's audit chain.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import llm_dojo_scoring
from llm_dojo_scoring.archive import archive_entry_hash, canonical_json, verify_entry_hash
from llm_dojo_scoring.scorecard_honesty import SCORER_VERSION

from observability.suite_scoring import (
    is_unscorable,
    payload_extras,
    score_document_payload,
    score_with_suite,
)
from schemas.audit import build_audit_entry, compute_audit_hash

CLAIM = {
    "claim_number": "C-1",
    "insurer": "Acme",
    "insured_party": "Pat",
    "claim_type": "carrier",
    "coverage_determination": "denied",
    "denial_reasons": ["exclusion"],
    "claimed_amount": 10.0,
}


def test_score_document_payload_carries_format_metric_id_and_provenance():
    payload = score_document_payload(
        "insurance_claim", CLAIM, CLAIM, dataset_revision="v9.1"
    )
    assert payload is not None and not is_unscorable(payload)
    assert payload["extraction"].overall_score == 1.0
    assert payload["metric_id"]
    assert payload["parse_ok"] == 1.0
    assert 0.0 <= payload["schema_adherence"] <= 1.0
    assert payload["provenance"]["dataset_revision"] == "v9.1"
    # The provenance version is the installed library's own version, so the
    # assertion follows the pyproject pin instead of a hardcoded series.
    assert payload["provenance"]["scorer_version"] == SCORER_VERSION == llm_dojo_scoring.__version__


def test_payload_extras_keep_registry_names_and_drop_format_fractions():
    payload = score_document_payload("insurance_claim", CLAIM, CLAIM)
    extras = payload_extras(payload)
    assert extras["extraction_f1"] == 1.0
    assert extras["determination_consistency"] == 1.0
    # Dojo's fractional schema_valid must not collide with the pipeline's
    # boolean schema_valid trace score.
    assert "schema_valid" not in extras
    assert "parse_ok" not in extras


def test_unscorable_gt_fails_closed():
    # Triage-only contract GT has nothing extractable: dojo marks it
    # unscorable and mailroom must not fall back to a phantom score.
    payload = score_document_payload("contract", {}, {"doc_subtype": "nda"})
    assert is_unscorable(payload)
    result, extras = score_with_suite("contract", {}, {"doc_subtype": "nda"})
    assert result.overall_score is None
    assert result.field_scores == {}
    assert extras == {}


def test_hf_pilot_row_records_scorecard_identity():
    from scripts.run_hf_pilot import DATASET_REVISION, score_row_extraction

    out = score_row_extraction(CLAIM, CLAIM, "insurance_claim")
    assert out["overall_score"] == 1.0
    assert out["metric_id"]
    assert out["scorer_version"] == SCORER_VERSION == llm_dojo_scoring.__version__
    assert out["dataset_revision"] == DATASET_REVISION
    assert out["format_parse_ok"] == 1.0


def test_hf_pilot_row_unscorable_keeps_reason():
    from scripts.run_hf_pilot import score_row_extraction

    out = score_row_extraction({"parties": ["A"]}, {"doc_subtype": "nda"}, "contract")
    assert out["overall_score"] is None
    assert out["extraction_status"] == "unscorable"
    assert out["unscorable_reason"]


def test_dojo_archive_hash_matches_mailroom_audit_chain():
    """Match dojo and mailroom audit hashes for string and datetime timestamps."""
    ts = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    detail = {"stage": "archived", "scoring": {"overall_score": 0.9, "note": "é"}}
    kwargs = dict(
        prev_hash="0" * 64,
        doc_id="doc-1",
        entry_id="e-1",
        event="archived",
        detail=detail,
        matter_id="m-1",
        actor="archivist",
    )
    mailroom = compute_audit_hash(timestamp=ts, **kwargs)
    assert archive_entry_hash(timestamp=ts.isoformat(), **kwargs) == mailroom
    assert archive_entry_hash(timestamp=ts, **kwargs) == mailroom


_TRICKY_DETAILS = [
    {},
    {"stage": "archived", "scoring": {"overall_score": 0.9, "note": "é ✓ 文"}},
    {"z": 1, "a": [3, 2, {"y": None, "b": True}], "m": {"k": 1.0, "j": -0.5}},
    {"when": datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc), "day": date(2026, 10, 6)},
    {"amount": Decimal("12.50"), "blob": b"raw", "set_like": ["b", "a"]},
]


@pytest.mark.parametrize("detail", _TRICKY_DETAILS)
def test_dojo_canonical_json_is_mailroom_audit_serialization(detail):
    """Byte-compat guard: dojo ``canonical_json`` == the exact
    ``json.dumps(..., sort_keys=True, default=str)`` call in
    ``schemas.audit.compute_audit_hash`` (default separators, ASCII-escaped,
    ``default=str``). Any drift on either side breaks cross-package
    ``verify_chain``."""
    assert canonical_json(detail) == json.dumps(detail, sort_keys=True, default=str)


@pytest.mark.parametrize("detail", _TRICKY_DETAILS)
def test_dojo_archive_hash_parity_on_tricky_details(detail):
    """Preserve audit hash parity for nested, Unicode, and non-JSON details."""
    ts = datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=timezone.utc)
    kwargs = dict(
        prev_hash="a" * 64,
        doc_id="doc-é",
        entry_id="e-2",
        event="archived",
        detail=detail,
        matter_id="m-1",
        actor="archivist",
    )
    assert archive_entry_hash(timestamp=ts, **kwargs) == compute_audit_hash(
        timestamp=ts, **kwargs
    )


def test_mailroom_built_entry_verifies_under_dojo():
    """Verify a mailroom audit entry with dojo and detect subsequent tampering."""
    entry = build_audit_entry(
        "doc-1", "m-1", "archived", "archivist", {"stage": "archived", "n": 1}
    )
    row = {
        "prev_hash": entry.prev_hash,
        "doc_id": entry.doc_id,
        "entry_id": entry.entry_id,
        "matter_id": entry.matter_id,
        "actor": entry.actor,
        "timestamp": entry.timestamp.isoformat(),
        "event": entry.event,
        "detail": entry.detail,
        "entry_hash": entry.entry_hash,
    }
    assert verify_entry_hash(row)
    row["detail"] = {"stage": "archived", "n": 2}
    assert not verify_entry_hash(row)
