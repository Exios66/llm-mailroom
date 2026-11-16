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

    @pytest.mark.parametrize(
        ("raw_score", "expected"),
        [('7.0', 1.0), ('-0.5', 0.0), ('null', 0.0), ('"n/a"', 0.0), ('[0.5]', 0.0)],
    )
    def test_judge_completeness_guards_and_clamps_score(
        self, mock_openai_client, sample_contract_text, raw_score, expected
    ):
        """Match the sibling judges: a non-numeric score falls back to 0.0
        instead of raising, and out-of-range scores clamp to [0, 1]."""
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            f'{{"completeness": {raw_score}, "completeness_label": "partial", "reasoning": "r"}}'
        )
        from agents.judge import CompletenessJudge
        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        result = judge.judge_completeness("contract", {"parties": ["ACME"]}, sample_contract_text[:1000])
        assert result["completeness"] == expected


class TestJudgeIgnoresPipelineMetadata:
    """#242: ``confidence`` / ``reasoning`` / ``_``-keys are pipeline metadata, not content."""

    EXTRACTED = {
        "sender": "Acme",
        "confidence": 0.93,
        "reasoning": {"sender": "from the letterhead"},
        "_report": "meta",
    }

    @staticmethod
    def _judge(mock_openai_client, content):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = content
        from agents.judge import CompletenessJudge

        judge = CompletenessJudge()
        judge.client = mock_openai_client
        judge.model = "test-model"
        return judge

    @staticmethod
    def _user_message(mock_openai_client) -> str:
        kwargs = mock_openai_client.chat.completions.create.call_args.kwargs
        return next(m["content"] for m in kwargs["messages"] if m["role"] == "user")

    @pytest.mark.parametrize(
        ("doc_type", "content_field"),
        [
            ("contract", "parties"),
            ("merger_agreement", "parties"),
            ("correspondence", "sender"),
            ("insurance_claim", "claim_number"),
        ],
    )
    def test_field_list_excludes_metadata(self, doc_type, content_field):
        from agents.judge import CompletenessJudge

        listed = CompletenessJudge._field_list(doc_type)
        assert "  - confidence:" not in listed
        assert "  - reasoning:" not in listed
        assert f"  - {content_field}:" in listed

    def test_field_list_keeps_content_fields(self):
        from agents.judge import CompletenessJudge

        listed = CompletenessJudge._field_list("correspondence")
        assert "  - sender:" in listed
        assert "  - urgency:" in listed

    def test_completeness_payload_excludes_metadata(self, mock_openai_client):
        judge = self._judge(
            mock_openai_client, '{"completeness": 1.0, "completeness_label": "complete", "reasoning": "ok"}'
        )
        judge.judge_completeness("correspondence", dict(self.EXTRACTED), "Dear Sir")
        message = self._user_message(mock_openai_client)
        assert "confidence" not in message
        assert "from the letterhead" not in message
        assert "_report" not in message
        assert "'sender': 'Acme'" in message

    def test_correctness_payload_excludes_metadata(self, mock_openai_client):
        judge = self._judge(
            mock_openai_client,
            '{"extraction_correctness": 1.0, "extraction_correctness_label": "accurate", "reasoning": "ok"}',
        )
        judge.judge_extraction_correctness("correspondence", dict(self.EXTRACTED), "Dear Sir")
        message = self._user_message(mock_openai_client)
        assert "confidence" not in message
        assert "from the letterhead" not in message
        assert "_report" not in message
        assert "'sender': 'Acme'" in message

    def test_caller_extraction_is_not_mutated(self, mock_openai_client):
        judge = self._judge(
            mock_openai_client, '{"completeness": 1.0, "completeness_label": "complete", "reasoning": "ok"}'
        )
        extracted = dict(self.EXTRACTED)
        judge.judge_completeness("correspondence", extracted, "Dear Sir")
        assert extracted == self.EXTRACTED
