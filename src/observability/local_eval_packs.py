"""Local eval packs that close honesty gaps Hub cannot.

Hub `docclass-merged` still has:

* CMS insurance rows that are all ``approved`` / empty ``denial_reasons``
* zero ``compliance_filing`` rows
* ``corporate_record`` subclass labels only (no CUAD/MAUD-grade field gold)

These packs score committed in-repo fixtures with schema-complete
``expected_fields``. They are mock/check-only (synthetic text — never billed
as Hub ``--real`` accuracy). Perfect-extract scores are scorer self-checks,
not model quality.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Insurance contrast: mixed coverage_determination so determination_consistency
# is not the Hub CMS tautology (always-approved / empty denials).
_INSURANCE_CONTRAST: tuple[dict[str, Any], ...] = (
    {
        "filename": "sample_claim_approved.txt",
        "expected_hf_class": "insurance_claim",
        "expected_subclass": "property",
        "expected_fields": {
            "claim_number": "2026-CLM-041702",
            "policy_number": "HO-44-88391-A",
            "insurer": "Acme Insurance Company",
            "insured_party": "Jack B, Morningstar Collective LLC",
            "claim_type": "property",
            "date_of_loss": "2026-03-14",
            "date_filed": "2026-03-21",
            "claimed_amount": 18530.0,
            "adjuster": "J. Featherstone",
            "damages_description": (
                "hailstorm damaged the asphalt shingle roof and the detached garage"
            ),
            "coverage_determination": "approved",
            "denial_reasons": [],
            "supporting_documents": [
                "photos of roof and garage damage",
                "contractor estimate from R. Carter Roofing dated March 19, 2026",
                "adjuster inspection report dated April 2, 2026",
            ],
        },
    },
    {
        "filename": "sample_claim_denied.txt",
        "expected_hf_class": "insurance_claim",
        "expected_subclass": "auto",
        "expected_fields": {
            "claim_number": "2026-CLM-055810",
            "policy_number": "AUTO-19-22014-B",
            "insurer": "Acme Insurance Company",
            "insured_party": "Riley Chen",
            "claim_type": "auto",
            "date_of_loss": "2026-02-02",
            "date_filed": "2026-02-05",
            "claimed_amount": 4280.0,
            "adjuster": "M. Okonkwo",
            "damages_description": (
                "2019 Honda Civic struck a parking barrier; front bumper and "
                "headlamp assembly damaged"
            ),
            "coverage_determination": "denied",
            "denial_reasons": [
                "Policy exclusion: collision coverage lapsed for non-payment "
                "effective January 15, 2026; the loss occurred after the lapse."
            ],
            "supporting_documents": [
                "photos of bumper damage",
                "declarations page showing lapse",
                "cancellation notice dated January 10, 2026",
            ],
        },
    },
    {
        "filename": "sample_claim_partial.txt",
        "expected_hf_class": "insurance_claim",
        "expected_subclass": "property",
        "expected_fields": {
            "claim_number": "2026-CLM-062203",
            "policy_number": "HO-44-99012-C",
            "insurer": "Acme Insurance Company",
            "insured_party": "Priya Nair",
            "claim_type": "property",
            "date_of_loss": "2026-01-08",
            "date_filed": "2026-01-12",
            "claimed_amount": 11400.0,
            "adjuster": "S. Alvarez",
            "damages_description": (
                "supply-line failure under the kitchen sink flooded the first floor"
            ),
            "coverage_determination": "partial",
            "denial_reasons": [
                "Wear-and-tear / betterment: dishwasher replacement is not caused "
                "by the covered water loss and is excluded as maintenance/betterment."
            ],
            "supporting_documents": [
                "plumber invoice dated January 9, 2026",
                "photos of affected rooms",
                "contents inventory including dishwasher",
            ],
        },
    },
)

_COMPLIANCE_LOCAL: tuple[dict[str, Any], ...] = (
    {
        "filename": "sample_10k.txt",
        "expected_hf_class": "compliance_filing",
        "expected_subclass": "10-K",
        "expected_fields": {
            "filing_type": "10-K",
            "regulatory_body": "SEC",
            "filing_date": None,
            "due_date": None,
            "entity_name": "NovaTech Solutions, Inc.",
            "key_requirements": [
                "Annual report pursuant to Section 13 or 15(d)",
                "Documents Incorporated by Reference: Portions of the definitive Proxy Statement",
            ],
            "status": "filed",
            "reference_number": "001-98765",
        },
    },
    {
        "filename": "sample_state_filing.txt",
        "expected_hf_class": "compliance_filing",
        # State annual report is not an SEC form body; Hub catalog residual.
        "expected_subclass": "other",
        "expected_fields": {
            "filing_type": "other",
            "regulatory_body": "Delaware Division of Corporations",
            "filing_date": "2024-05-15",
            "due_date": "2024-06-30",
            "entity_name": "Meridian Holdings, Inc.",
            "key_requirements": [
                "Annual Franchise Tax Payment: $75,000",
                "Updated Director and Officer List: Attached",
                "Business Activity Certification: Completed",
            ],
            "status": "filed",
            "reference_number": "DE-2023-884721",
        },
    },
)

_CORPORATE_EXTRACTION: tuple[dict[str, Any], ...] = (
    {
        "filename": "sample_bylaws.txt",
        "expected_hf_class": "corporate_record",
        "expected_subclass": "bylaws",
        "expected_fields": {
            "entity_name": "Meridian Holdings, Inc.",
            "record_type": "bylaws",
            "effective_date": "2023-02-01",
            "key_provisions": [
                "The annual meeting of stockholders shall be held on the second Tuesday of May",
                "The Board shall consist of not less than three (3) nor more than nine (9) directors",
                "The officers of the Corporation shall include a President, a Secretary, and a Treasurer",
            ],
            "signatories": ["Thomas Meridian", "Elizabeth Warren"],
            "jurisdiction": "Delaware",
            "filing_number": "DE-2023-884721",
        },
    },
    {
        "filename": "sample_resolution.txt",
        "expected_hf_class": "corporate_record",
        # Hub extract inventory is five tokens; board resolutions map to other.
        "expected_subclass": "other",
        "expected_fields": {
            "entity_name": "Meridian Holdings, Inc.",
            "record_type": "other",
            "effective_date": "2024-03-15",
            "key_provisions": [
                "Authorization of Series B Preferred Stock Financing",
                "Certificate of Designation for the Series B Preferred Stock is approved",
                "officers are authorized to file the Certificate of Designation with the Delaware Secretary of State",
            ],
            "signatories": ["Thomas Meridian", "Elizabeth Warren", "James Chen"],
            "jurisdiction": "Delaware",
            "filing_number": None,
        },
    },
)

_PACK_FIXTURE_DIR = {
    "insurance_claim": "insurance_claim",
    "compliance_filing": "compliance_filing",
    "corporate_record": "corporate_record",
}


def _read_fixture(doc_class: str, filename: str) -> str:
    path = _FIXTURES / _PACK_FIXTURE_DIR[doc_class] / filename
    return path.read_text(encoding="utf-8")


def _hydrate(spec: dict[str, Any]) -> dict[str, Any]:
    doc_class = str(spec["expected_hf_class"])
    text = _read_fixture(doc_class, spec["filename"])
    fields = {
        k: v for k, v in (spec.get("expected_fields") or {}).items() if v not in (None, "")
    }
    return {
        "filename": spec["filename"],
        "text": text,
        "chars": len(text),
        "expected_hf_class": doc_class,
        "expected_subclass": spec.get("expected_subclass") or "",
        "expected_fields": fields,
        "pack": True,
        "mock_only": True,
    }


def insurance_contrast_samples() -> list[dict[str, Any]]:
    return [_hydrate(spec) for spec in _INSURANCE_CONTRAST]


def compliance_local_samples() -> list[dict[str, Any]]:
    return [_hydrate(spec) for spec in _COMPLIANCE_LOCAL]


def corporate_extraction_samples() -> list[dict[str, Any]]:
    return [_hydrate(spec) for spec in _CORPORATE_EXTRACTION]


def all_local_pack_samples() -> list[dict[str, Any]]:
    """Fixture samples for ``--mock`` (additive; never mixed into Hub ``--real``)."""
    return (
        insurance_contrast_samples()
        + compliance_local_samples()
        + corporate_extraction_samples()
    )


def _score_one(doc_class: str, predicted: dict, expected: dict) -> dict[str, Any]:
    from observability.field_scoring import get_field_types
    from observability.suite_scoring import score_with_suite

    result, extras = score_with_suite(
        doc_class,
        predicted,
        expected,
        field_types=get_field_types(doc_class),
    )
    overall = result.overall_score
    out: dict[str, Any] = {
        "overall_score": None if overall is None else round(float(overall), 3),
        "n_fields": len(result.field_scores or {}),
    }
    for key, value in extras.items():
        out[key] = round(float(value), 3)
    return out


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _perfect_extract_summary(samples: list[dict[str, Any]], doc_class: str) -> dict[str, Any]:
    rows = []
    for sample in samples:
        expected = dict(sample.get("expected_fields") or {})
        scored = _score_one(doc_class, expected, expected)
        rows.append(scored)
    overall = [r["overall_score"] for r in rows if isinstance(r.get("overall_score"), (int, float))]
    consistency = [
        r["determination_consistency"]
        for r in rows
        if isinstance(r.get("determination_consistency"), (int, float))
    ]
    f1 = [r["extraction_f1"] for r in rows if isinstance(r.get("extraction_f1"), (int, float))]
    return {
        "n": len(rows),
        "extraction_overall_mean": _mean(overall),
        "extraction_f1_mean": _mean(f1),
        "determination_consistency_mean": _mean(consistency),
        "kind": "scorer_self_check",
    }


def score_local_packs() -> dict[str, Any]:
    """Deterministic pack scores for ``--check`` and HF ``report.json``.

    Perfect extracts are labelled ``scorer_self_check`` so they cannot be
    mistaken for Hub model accuracy. Insurance also scores the denied-without-
    reasons adversarial case so ``determination_consistency`` is shown to
    move off 1.0.
    """
    insurance = insurance_contrast_samples()
    determinations = [
        str((s.get("expected_fields") or {}).get("coverage_determination") or "")
        for s in insurance
    ]
    denied = next(
        s for s in insurance
        if (s.get("expected_fields") or {}).get("coverage_determination") == "denied"
    )
    denied_fields = dict(denied["expected_fields"])
    adversarial_pred = dict(denied_fields)
    adversarial_pred["denial_reasons"] = []
    adversarial = _score_one("insurance_claim", adversarial_pred, denied_fields)

    from observability.honest_gaps import insurance_expected_set_is_homogeneous

    cms_shaped = [
        {
            "coverage_determination": "approved",
            "denial_reasons": [],
            "claimed_amount": 110.0,
        }
        for _ in range(3)
    ]
    return {
        "insurance_contrast": {
            "doc_class": "insurance_claim",
            "source": "local",
            "mock_only": True,
            "n": len(insurance),
            "determinations": determinations,
            "gt_homogeneity": insurance_expected_set_is_homogeneous(
                [s.get("expected_fields") or {} for s in insurance]
            ),
            "perfect_extract": _perfect_extract_summary(insurance, "insurance_claim"),
            "adversarial_denied_without_reasons": {
                "determination_consistency": adversarial.get("determination_consistency"),
                "kind": "scorer_self_check",
            },
            "hub_cms_shaped": {
                "gt_homogeneity": insurance_expected_set_is_homogeneous(cms_shaped),
                "determination_consistency_is_quality": False,
            },
        },
        "compliance_filing": {
            "doc_class": "compliance_filing",
            "source": "local",
            "in_hub": False,
            "in_hf_pilot": False,
            "mock_only": True,
            "n": len(_COMPLIANCE_LOCAL),
            "subclasses": [s["expected_subclass"] for s in _COMPLIANCE_LOCAL],
            "perfect_extract": _perfect_extract_summary(
                compliance_local_samples(), "compliance_filing"
            ),
        },
        "corporate_extraction": {
            "doc_class": "corporate_record",
            "source": "local",
            "in_hub": True,
            "hub_extract_is_subclass_only": True,
            "mock_only": True,
            "n": len(_CORPORATE_EXTRACTION),
            "schema_fields": sorted(
                {
                    key
                    for spec in _CORPORATE_EXTRACTION
                    for key in (spec.get("expected_fields") or {})
                    if (spec.get("expected_fields") or {}).get(key) not in (None, "")
                }
            ),
            "perfect_extract": _perfect_extract_summary(
                corporate_extraction_samples(), "corporate_record"
            ),
        },
    }


def local_pack_status(doc_class: str) -> dict[str, Any]:
    """Honesty-table extras for one taxonomy class."""
    kind = str(doc_class or "")
    if kind == "insurance_claim":
        return {
            "local_pack": "insurance_contrast",
            "local_pack_mock_only": True,
            "hub_gt_homogeneous": True,
        }
    if kind == "compliance_filing":
        return {
            "local_pack": "compliance_filing",
            "local_pack_mock_only": True,
            "in_hub": False,
        }
    if kind == "corporate_record":
        return {
            "local_pack": "corporate_extraction",
            "local_pack_mock_only": True,
            "hub_extract_is_subclass_only": True,
        }
    return {}
