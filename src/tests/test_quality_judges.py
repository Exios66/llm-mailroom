from scripts import run_quality_judges


def _sample(extracted=None):
    return {
        "id": "sample-01",
        "doc_type": "contract",
        "extracted_data": extracted if extracted is not None else {"parties": ["ACME"]},
        "subdir": "contract",
        "filename": "sample-01.pdf",
    }


def test_judge_one_runs_three_independent_dimensions(monkeypatch):
    monkeypatch.setattr(run_quality_judges, "_raw_text_for", lambda sample: "Agreement text")

    result = run_quality_judges.judge_one(
        _sample(), mock_mode=True, judges=run_quality_judges.JUDGES
    )

    assert result["status"] == "judged"
    assert set(result) >= {"classification", "completeness", "correctness"}
    assert result["classification"]["classification_correct"] == "correct"
    assert result["completeness"]["completeness_label"] == "complete"
    assert result["correctness"]["extraction_correctness_label"] == "accurate"


def test_judge_dimension_failure_does_not_block_other_dimensions(monkeypatch):
    monkeypatch.setattr(run_quality_judges, "_raw_text_for", lambda sample: "Agreement text")

    class FakeJudge:
        instances = 0

        def __init__(self):
            FakeJudge.instances += 1

        def judge_classification(self, doc_type, doc_text):
            raise RuntimeError("classification unavailable")

        def judge_completeness(self, doc_type, extracted, doc_text):
            return {"completeness": 0.5, "completeness_label": "partial", "reasoning": "ok"}

        def judge_extraction_correctness(self, doc_type, extracted, doc_text):
            return {
                "extraction_correctness": 0.5,
                "extraction_correctness_label": "partial",
                "reasoning": "ok",
            }

    monkeypatch.setattr("agents.judge.CompletenessJudge", FakeJudge)
    result = run_quality_judges.judge_one(
        _sample(), mock_mode=False, judges=run_quality_judges.JUDGES
    )

    assert FakeJudge.instances == 3
    assert "classification" not in result
    assert result["completeness"]["completeness_label"] == "partial"
    assert result["correctness"]["extraction_correctness_label"] == "partial"
    assert "classification" in result["errors"]


def test_raw_text_prefers_inline_doc_text():
    sample = _sample()
    sample["doc_text"] = "inline source"
    assert run_quality_judges._raw_text_for(sample) == "inline source"


def test_hf_report_mock_judge(tmp_path, monkeypatch):
    report = {
        "samples": [{
            "local_filename": "hf_contract.txt",
            "filename": "hf_contract.txt",
            "predicted": "contract",
            "expected_doc_class": "contract",
            "expected": "contract",
            "extracted_data": {"parties": ["Acme"]},
            "trace_id": "trace-hf-1",
            "doc_text": "SERVICES AGREEMENT between Acme and Beta.",
        }]
    }
    path = tmp_path / "report.json"
    path.write_text(__import__("json").dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["run_quality_judges.py", "--mock", "--hf-report", str(path)],
    )
    assert run_quality_judges.main() == 0
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    evaluation = payload["evaluation"]["run"]
    assert evaluation["hf_mode"] is True
    assert evaluation["summary"]["classification"]["n"] == 1

