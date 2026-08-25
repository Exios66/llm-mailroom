import pytest
from unittest.mock import MagicMock

from langchain_agents.mock import FakeLangChainLLM


class _ParseErrorLangChainLLM(FakeLangChainLLM):
    """Fake whose structured runner reports a parsing_error (the vendored
    _call_structured fallback path)."""

    class _Runner:
        def invoke(self, messages, **kwargs):
            raw = MagicMock()
            raw.content = "not valid json {{{{{{"
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("boom")}

    def with_structured_output(self, json_schema, **kwargs):
        return self._Runner()


class TestSorterAgent:
    def test_classify_contract(self, sample_contract_text, mock_langchain_llm):
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "service",
            "confidence": 0.95,
            "reasoning": "Standard MSA structure",
        }
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_contract_text[:1000]
        )
        assert doc_type == "contract"
        assert contract_subtype == "service"
        assert confidence >= 0.90

    def test_classify_corporate_record(self, sample_corporate_text, mock_langchain_llm):
        mock_langchain_llm.classification = {
            "doc_type": "corporate_record",
            "contract_subtype": None,
            "confidence": 0.92,
            "reasoning": "Bylaws document",
        }
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_corporate_text[:1000]
        )
        assert doc_type == "corporate_record"
        assert contract_subtype is None
        assert confidence >= 0.80

    def test_classify_low_confidence(self, sample_ambiguous_text, mock_langchain_llm):
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "other",
            "confidence": 0.45,
            "reasoning": "Ambiguous content",
        }
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_ambiguous_text[:1000]
        )
        assert confidence < 0.70

    def test_classify_returns_valid_enum(self, sample_contract_text, mock_langchain_llm):
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "license",
            "confidence": 0.88,
            "reasoning": "Clear contract",
        }
        from agents.sorter import SorterAgent
        from pipeline.config import get_all_doc_types

        agent = SorterAgent()
        doc_type, _, _, _ = agent.classify(sample_contract_text[:1000])
        valid_types = get_all_doc_types()
        assert doc_type in valid_types

    def test_classify_normalizes_subtype_label(self, sample_contract_text, mock_langchain_llm):
        # The model sometimes returns a label instead of a key; normalize_subtype
        # must coerce it to a canonical key.
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "License Agreement",
            "confidence": 0.85,
            "reasoning": "Grant of license",
        }
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_contract_text[:1000]
        )
        assert contract_subtype == "license"

    def test_classify_invalid_doc_type_is_not_silently_remapped(
        self, sample_contract_text, mock_langchain_llm
    ):
        # A hallucinated class must reach the graph as-is. Remapping it onto
        # correspondence at the model's 0.8 confidence used to auto-extract
        # garbage as a letter.
        mock_langchain_llm.classification = {
            "doc_type": "not_a_doc_type",
            "contract_subtype": "license",
            "confidence": 0.8,
            "reasoning": "nonsense",
        }
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_contract_text[:1000]
        )
        assert doc_type == "not_a_doc_type"
        assert contract_subtype is None
        assert confidence == 0.8

    def test_classify_parse_error(self, sample_contract_text, mock_langchain_llm, mocker):
        from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent

        mocker.patch.object(_LangChainBaseAgent, "llm", new=lambda self: _ParseErrorLangChainLLM())
        from agents.sorter import SorterAgent

        agent = SorterAgent()
        doc_type, contract_subtype, confidence, reasoning = agent.classify(
            sample_contract_text[:1000]
        )
        assert confidence <= 0.5
        assert doc_type == "correspondence"
        assert contract_subtype is None

    def test_classify_image_invalid_doc_type_is_not_silently_remapped(self, mocker):
        from langchain_agents.sorter_agent import SorterAgent

        agent = SorterAgent()
        mocker.patch.object(
            agent,
            "_call_vision",
            return_value=(
                "<label>court_opinion</label>\n"
                "<confidence>91</confidence>\n"
                "<reasoning>judicial caption</reasoning>"
            ),
        )
        result = agent.classify_image("fake")
        assert result["doc_type"] == "court_opinion"
        assert result["confidence"] == 0.91

    def test_classify_image_unknown_label_is_preserved(self, mocker):
        from langchain_agents.sorter_agent import SorterAgent

        agent = SorterAgent()
        mocker.patch.object(
            agent,
            "_call_vision",
            return_value=(
                "<label>unknown</label>\n"
                "<confidence>88</confidence>\n"
                "<reasoning>no live class fits</reasoning>"
            ),
        )
        result = agent.classify_image("fake")
        assert result["doc_type"] == "unknown"
        assert result["confidence"] == 0.88

    def test_classify_document_empty_pages_is_unknown(self):
        from langchain_agents.sorter_agent import SorterAgent

        agent = SorterAgent()
        result = agent.classify_document([])
        assert result["doc_type"] == "unknown"
        assert result["confidence"] == 0.0

    def test_sorter_schema_enum_includes_unknown(self):
        from langchain_agents.sorter_agent import SORTER_SCHEMA, DOC_CLASS_KEYS

        enum = SORTER_SCHEMA["properties"]["doc_type"]["enum"]
        assert "unknown" in enum
        for key in DOC_CLASS_KEYS:
            assert key in enum
        assert "court_opinion" not in enum
        assert "due_diligence" not in enum
