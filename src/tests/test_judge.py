import pytest


class TestClassificationJudge:
    def test_judge_classification(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"classification_correct": "correct", "classification_quality": 0.9, '
            '"reasoning": "Standard MSA structure"}'
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_classification("contract", sample_contract_text[:1000], reasoning="looks like an MSA")
        assert result["classification_correct"] == "correct"
        assert 0 <= result["classification_quality"] <= 1

    def test_judge_parse_error_defaults_to_ambiguous(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            "not json {{{{{"
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_classification("contract", sample_contract_text[:1000])
        assert result["classification_correct"] == "ambiguous"
        assert result["classification_quality"] == 0.0


class TestExtractionCorrectnessJudge:
    def test_judge_correctness(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"extraction_correctness": 0.8, "extraction_correctness_label": "partial", '
            '"reasoning": "one value unsupported"}'
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_extraction_correctness("contract", {"parties": ["ACME"]}, sample_contract_text[:1000])
        assert result["extraction_correctness_label"] == "partial"
        assert result["extraction_correctness"] == 0.8

    def test_judge_correctness_clamps_out_of_range(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"extraction_correctness": 7.0, "extraction_correctness_label": "accurate", '
            '"reasoning": "ok"}'
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_extraction_correctness("contract", {"parties": ["ACME"]}, sample_contract_text[:1000])
        assert result["extraction_correctness"] == 1.0

    def test_judge_correctness_parse_error(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            "garbage {{{"
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_extraction_correctness("contract", {"parties": ["ACME"]}, sample_contract_text[:1000])
        assert result["extraction_correctness_label"] == "inaccurate"


class TestCompletenessJudge:
    def test_judge_completeness(self, mock_openai_client, sample_contract_text):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"completeness": 0.6, "completeness_label": "partial", "reasoning": "missing parties"}'
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_completeness("contract", {"parties": ["ACME"]}, sample_contract_text[:1000])
        assert result["completeness_label"] == "partial"
        assert result["completeness"] == 0.6
