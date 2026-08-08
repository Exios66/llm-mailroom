import json
import pytest
from unittest.mock import patch, MagicMock


class TestSorterAgent:
    def test_classify_contract(self, sample_contract_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "contract", "confidence": 0.95, "reasoning": "Standard MSA structure"}'
        )
        from agents.sorter import SorterAgent
        agent = SorterAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        doc_type, confidence, reasoning = agent.classify(sample_contract_text[:1000])
        assert doc_type == "contract"
        assert confidence >= 0.90

    def test_classify_corporate_record(self, sample_corporate_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "corporate_record", "confidence": 0.92, "reasoning": "Bylaws document"}'
        )
        from agents.sorter import SorterAgent
        agent = SorterAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        doc_type, confidence, reasoning = agent.classify(sample_corporate_text[:1000])
        assert doc_type == "corporate_record"
        assert confidence >= 0.80

    def test_classify_low_confidence(self, sample_ambiguous_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "contract", "confidence": 0.45, "reasoning": "Ambiguous content"}'
        )
        from agents.sorter import SorterAgent
        agent = SorterAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        doc_type, confidence, reasoning = agent.classify(sample_ambiguous_text[:1000])
        assert confidence < 0.70

    def test_classify_returns_valid_enum(self, sample_contract_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "contract", "confidence": 0.88, "reasoning": "Clear contract"}'
        )
        from agents.sorter import SorterAgent
        from pipeline.config import get_all_doc_types
        agent = SorterAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        doc_type, _, _ = agent.classify(sample_contract_text[:1000])
        valid_types = get_all_doc_types()
        assert doc_type in valid_types

    def test_classify_json_parse_error(self, sample_contract_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            "not valid json {{{{{{"
        )
        from agents.sorter import SorterAgent
        agent = SorterAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        doc_type, confidence, reasoning = agent.classify(sample_contract_text[:1000])
        assert confidence <= 0.5
