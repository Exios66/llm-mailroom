import pytest


class TestGuardClassification:
    def test_valid_classification_passes(self):
        from pipeline.guards import guard_classification

        guard = guard_classification(
            {
                "doc_type": "contract",
                "contract_subtype": "license",
                "classification_confidence": 0.9,
            }
        )
        assert guard["ok"] is True
        assert guard["issues"] == []
        assert guard["confidence"] == 0.9

    def test_unknown_doc_type_fails(self):
        from pipeline.guards import guard_classification

        guard = guard_classification({"doc_type": "not_a_type", "classification_confidence": 0.9})
        assert guard["ok"] is False
        assert any("unknown_doc_type" in i for i in guard["issues"])

    def test_merger_agreement_alias_passes(self):
        from pipeline.guards import guard_classification

        guard = guard_classification({
            "doc_type": "merger_agreement",
            "classification_confidence": 0.97,
        })
        assert guard["ok"] is True
        assert guard["issues"] == []

    def test_out_of_range_confidence_fails(self):
        from pipeline.guards import guard_classification

        guard = guard_classification({"doc_type": "contract", "classification_confidence": 7})
        assert guard["ok"] is False
        assert any("out_of_range" in i for i in guard["issues"])
        assert "confidence" not in guard

    def test_missing_confidence_passes(self):
        from pipeline.guards import guard_classification

        guard = guard_classification(
            {"doc_type": "contract", "contract_subtype": "other"}
        )
        assert guard["ok"] is True

    def test_contract_missing_subtype_fails(self):
        from pipeline.guards import guard_classification

        guard = guard_classification(
            {"doc_type": "contract", "classification_confidence": 0.9}
        )
        assert guard["ok"] is False
        assert any("contract_subtype_missing" in i for i in guard["issues"])

    def test_contract_unknown_subtype_fails(self):
        from pipeline.guards import guard_classification

        guard = guard_classification(
            {
                "doc_type": "contract",
                "contract_subtype": "bogus_family",
                "classification_confidence": 0.9,
            }
        )
        assert guard["ok"] is False
        assert any("contract_subtype_unknown" in i for i in guard["issues"])

    def test_non_contract_with_subtype_fails(self):
        from pipeline.guards import guard_classification

        guard = guard_classification(
            {
                "doc_type": "correspondence",
                "contract_subtype": "license",
                "classification_confidence": 0.9,
            }
        )
        assert guard["ok"] is False
        assert any("contract_subtype_not_null_for_non_contract" in i for i in guard["issues"])


class TestGuardExtraction:
    def test_valid_extraction_passes(self):
        from pipeline.guards import guard_extraction

        guard = guard_extraction(
            "contract",
            {
                "parties": ["ACME"],
                "effective_date": "2024-01-15",
                "term_length": "3 years",
                "termination_clauses": [],
                "governing_law": "Delaware",
                "key_obligations": ["uptime"],
                "contract_value": None,
                "renewal_terms": None,
            },
        )
        assert guard["ok"] is True
        assert guard["schema_valid"] is True

    def test_parse_error_fails(self):
        from pipeline.guards import guard_extraction

        guard = guard_extraction("contract", {"_parse_error": True})
        assert guard["ok"] is False
        assert guard["parse_error"] is True

    def test_schema_violation_fails(self):
        from pipeline.guards import guard_extraction

        # parties must be a list — a string violates the schema
        guard = guard_extraction("contract", {"parties": "ACME"})
        assert guard["ok"] is False
        assert any("schema_invalid" in i for i in guard["issues"])


class TestApplyExtractionGuard:
    def test_clamps_confidence_when_guard_fires(self):
        from pipeline.guards import apply_extraction_guard

        guard, confidence = apply_extraction_guard("contract", {"parties": "ACME"}, 0.95, attempts=1)
        assert guard["ok"] is False
        assert confidence == 0.5

    def test_keeps_confidence_when_ok(self):
        from pipeline.guards import apply_extraction_guard

        guard, confidence = apply_extraction_guard(
            "contract", {"parties": ["ACME"], "key_obligations": []}, 0.9, attempts=1
        )
        assert guard["ok"] is True
        assert confidence == 0.9

    def test_clamps_none_confidence_to_zero(self):
        from pipeline.guards import apply_extraction_guard

        guard, confidence = apply_extraction_guard("contract", {"_parse_error": True}, None, attempts=1)
        assert guard["ok"] is False
        assert confidence == 0.0


class TestApplyClassificationGuard:
    def test_clamps_valid_type_missing_subtype(self):
        from pipeline.guards import apply_classification_guard

        guard, confidence = apply_classification_guard(
            {
                "doc_type": "contract",
                "classification_confidence": 0.97,
            }
        )
        assert guard["ok"] is False
        assert confidence == 0.5

    def test_keeps_confidence_when_ok(self):
        from pipeline.guards import apply_classification_guard

        guard, confidence = apply_classification_guard(
            {
                "doc_type": "contract",
                "contract_subtype": "license",
                "classification_confidence": 0.9,
            }
        )
        assert guard["ok"] is True
        assert confidence == 0.9


class TestSubstantiveContent:
    def test_numeric_zero_is_content(self):
        from pipeline.guards import _has_substantive_content, guard_extraction

        assert _has_substantive_content({"claimed_amount": 0}) is True
        guard = guard_extraction(
            "insurance_claim",
            {"claimed_amount": 0, "claim_number": None},
        )
        assert "extraction_empty" not in guard["issues"]

    def test_empty_containers_are_not_content(self):
        from pipeline.guards import _has_substantive_content

        assert _has_substantive_content({"parties": [], "notes": "", "meta": None}) is False
        assert _has_substantive_content({"_parse_error": True, "reasoning": "x"}) is False
