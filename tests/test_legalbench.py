"""LegalBench suite tests — network-free, synthetic corpora only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legalbench import data as lb_data
from legalbench import scoring as lb_scoring
from legalbench.experiment_log import (
    append_record,
    build_record,
    default_log_path,
    regenerate,
)
from legalbench.mock import MockLegalBenchModel
from legalbench.prompts import get_prompt
from legalbench.runner import run_task
from legalbench.tasks import TASKS, get_task, task_help


def _cuad_payload() -> dict:
    """Tiny synthetic CUAD annotation file: 2 contracts x 3 categories."""
    contract_a = {
        "title": "ACME_2020-EX-10.1-DISTRIBUTOR AGREEMENT",
        "paragraphs": [
            {
                "context": "DISTRIBUTOR AGREEMENT between ACME and BETA. "
                           "This agreement may be terminated for convenience by either party.",
                "qas": [
                    {
                        "id": "ACME_2020-EX-10.1-DISTRIBUTOR AGREEMENT__Termination For Convenience",
                        "question": "Highlight the parts related to Termination For Convenience.",
                        "answers": [{"text": "terminated for convenience", "answer_start": 0}],
                        "is_impossible": False,
                    },
                    {
                        "id": "ACME_2020-EX-10.1-DISTRIBUTOR AGREEMENT__Non-Compete",
                        "question": "Highlight the parts related to Non-Compete.",
                        "answers": [],
                        "is_impossible": True,
                    },
                    {
                        "id": "ACME_2020-EX-10.1-DISTRIBUTOR AGREEMENT__Governing Law",
                        "question": "Highlight the parts related to Governing Law.",
                        "answers": [{"text": "governing law", "answer_start": 0}],
                        "is_impossible": False,
                    },
                ],
            }
        ],
    }
    contract_b = {
        "title": "GLOBALTECH_2019-EX-10.2-LICENSE AGREEMENT",
        "paragraphs": [
            {
                "context": "LICENSE AGREEMENT. Licensor grants a perpetual, irrevocable "
                           "license to use the licensed technology. This section is "
                           "padded out so the document text comfortably exceeds the "
                           "loader's minimum length filter for fixture realism.",
                "qas": [
                    {
                        "id": "GLOBALTECH_2019-EX-10.2-LICENSE AGREEMENT__License Grant",
                        "question": "Highlight the parts related to License Grant.",
                        "answers": [{"text": "perpetual, irrevocable license", "answer_start": 0}],
                        "is_impossible": False,
                    },
                    {
                        "id": "GLOBALTECH_2019-EX-10.2-LICENSE AGREEMENT__Non-Compete",
                        "question": "Highlight the parts related to Non-Compete.",
                        "answers": [],
                        "is_impossible": True,
                    },
                    {
                        "id": "GLOBALTECH_2019-EX-10.2-LICENSE AGREEMENT__Insurance",
                        "question": "Highlight the parts related to Insurance.",
                        "answers": [],
                        "is_impossible": True,
                    },
                ],
            }
        ],
    }
    return {"version": "v1", "data": [contract_a, contract_b]}


@pytest.fixture
def cuad_file(tmp_path: Path) -> Path:
    path = tmp_path / "CUAD_v1.json"
    path.write_text(json.dumps(_cuad_payload()), encoding="utf-8")
    return path


@pytest.fixture
def contracts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "contracts"
    d.mkdir()
    (d / "ACME_2020-EX-10.1-DISTRIBUTOR AGREEMENT.txt").write_text(
        "This is a long DISTRIBUTOR AGREEMENT between ACME and BETA " * 20,
        encoding="utf-8",
    )
    (d / "GLOBALTECH_2019-EX-10.2-LICENSE AGREEMENT.txt").write_text(
        "This is a long LICENSE AGREEMENT granting a perpetual license " * 20,
        encoding="utf-8",
    )
    return d


class TestDataLoaders:
    def test_cuad_qa_sampling_is_deterministic(self, cuad_file: Path):
        a = lb_data.load_cuad_qa(4, seed=7, cuad_path=cuad_file, min_text_chars=10)
        b = lb_data.load_cuad_qa(4, seed=7, cuad_path=cuad_file, min_text_chars=10)
        assert [r["qa_id"] for r in a] == [r["qa_id"] for r in b]
        assert len(a) == 4

    def test_cuad_qa_answers_and_evidence(self, cuad_file: Path):
        rows = lb_data.load_cuad_qa(6, seed=1, cuad_path=cuad_file, min_text_chars=10)
        by_cat = {r["category"]: r for r in rows}
        assert by_cat["Non-Compete"]["answer"] == "no"  # is_impossible
        assert by_cat["Termination For Convenience"]["answer"] == "yes"
        assert "terminated for convenience" in by_cat["Termination For Convenience"]["evidence"]
        assert len(by_cat["License Grant"]["document_text"]) > 100

    def test_cuad_qa_respects_contract_limit(self, cuad_file: Path):
        rows = lb_data.load_cuad_qa(10_000, seed=1, cuad_path=cuad_file, min_text_chars=10)
        assert len(rows) <= 6  # 2 contracts x 3 categories

    def test_cuad_missing_raises(self, tmp_path: Path):
        with pytest.raises(lb_data.CorpusUnavailable):
            lb_data.load_cuad_qa(1, seed=1, cuad_path=tmp_path / "nope.json")

    def test_family_rows_labeled(self, contracts_dir: Path):
        rows = lb_data.load_family_rows(2, seed=3, contracts_dir=contracts_dir)
        labels = sorted(r["family"] for r in rows)
        assert "distributor" in labels
        assert "license" in labels
        assert all(len(r["text"]) > 100 for r in rows)

    def test_family_missing_raises(self, tmp_path: Path):
        with pytest.raises(lb_data.CorpusUnavailable):
            lb_data.load_family_rows(1, seed=1, contracts_dir=tmp_path / "empty")


class TestScoring:
    def _binary_results(self):
        rows = []
        for cat, expected, predicted, conf in (
            ("A", "yes", "yes", 0.9),
            ("A", "no", "no", 0.8),
            ("B", "yes", "no", 0.6),
            ("B", "no", "yes", 0.7),
        ):
            rows.append({
                "status": "ok", "category": cat, "expected": expected,
                "predicted": predicted, "correct": predicted == expected,
                "confidence": conf,
            })
        return rows

    def test_score_binary_math(self):
        s = lb_scoring.score_binary(self._binary_results())
        assert s["accuracy"] == 0.5
        assert s["macro_category_accuracy"] == 0.5
        assert s["n_questions"] == 4 and s["n_yes"] == 2 and s["n_no"] == 2
        assert s["confidence_mean"] == 0.75
        assert 0 < s["calibration_error"] <= 1

    def test_score_binary_counts_errors(self):
        rows = self._binary_results() + [{"status": "error", "category": "A",
                                          "expected": "yes", "predicted": None,
                                          "correct": False, "confidence": None}]
        s = lb_scoring.score_binary(rows)
        assert s["n_error"] == 1

    def test_score_multiclass_equiv(self):
        rows = [
            {"status": "ok", "family": "maintenance", "expected": "maintenance",
             "predicted": "license", "correct": False, "confidence": 0.5},  # equivalent swap
            {"status": "ok", "family": "license", "expected": "license",
             "predicted": "license", "correct": True, "confidence": 0.8},
            {"status": "ok", "family": "distributor", "expected": "distributor",
             "predicted": "distributor", "correct": True, "confidence": 0.9},
        ]
        s = lb_scoring.score_multiclass(rows)
        assert s["accuracy"] == pytest.approx(2 / 3, abs=0.001)
        assert s["accuracy_equiv"] == pytest.approx(1.0, abs=0.001)  # maintenance<->license is a defensible swap
        assert 0 < s["macro_f1"] <= 1


class TestPromptsAndTasks:
    def test_prompt_versions_resolve(self):
        assert "yes" in get_prompt("legalbench_contract_qa_v1")
        fam = get_prompt("legalbench_family_classification_v1", TASKS["family_classification"].classes)
        assert "distributor" in fam and "other" in fam

    def test_registry(self):
        assert get_task("contract_qa").kind == "legalbench_binary_answer"
        assert get_task("family_classification").kind == "legalbench_multiclass_classification"
        assert "contract_qa" in task_help()
        with pytest.raises(KeyError):
            get_task("nope")


class TestRunner:
    def test_binary_mock_run(self, cuad_file: Path):
        rows = lb_data.load_cuad_qa(6, seed=1, cuad_path=cuad_file, min_text_chars=10)
        result = run_task("contract_qa", n=6, seed=1, rows=rows, mock=True,
                          trace_enabled=False)
        assert result.task_id == "contract_qa"
        assert len(result.results) == 6
        assert result.scores["n_questions"] == 6
        assert result.record["task"] == "legalbench_binary_answer"
        assert result.record["prompt_version"] == "legalbench_contract_qa_v1"
        assert result.record["model"] == "mock/mock-legalbench"
        assert result.record["data_source"]["seed"] == 1
        assert result.record["data_source"]["n_samples"] == 6
        assert result.record["git"]["commit"]
        assert result.record["tokens"]["total_tokens"] > 0
        # every result row carries the renderer's expected fields
        first = result.results[0]
        for key in ("filename", "expected", "predicted", "correct", "status"):
            assert key in first

    def test_family_mock_run(self, contracts_dir: Path):
        rows = lb_data.load_family_rows(2, seed=3, contracts_dir=contracts_dir)
        result = run_task("family_classification", n=2, seed=3, rows=rows,
                          mock=True, trace_enabled=False)
        assert result.scores["n_documents"] == 2
        assert result.record["task"] == "legalbench_multiclass_classification"

    def test_fingerprint_stable(self, cuad_file: Path):
        rows = lb_data.load_cuad_qa(4, seed=7, cuad_path=cuad_file)
        fp1 = build_record(task_id="contract_qa", kind="legalbench_binary_answer",
                           experiment_name="x", prompt_version="v", model="m",
                           rows=rows, n_requested=4, seed=7, scores={},
                           results=[], tokens={})["data_source"]["dataset_fingerprint"]
        fp2 = build_record(task_id="contract_qa", kind="legalbench_binary_answer",
                           experiment_name="x", prompt_version="v", model="m",
                           rows=rows, n_requested=4, seed=7, scores={},
                           results=[], tokens={})["data_source"]["dataset_fingerprint"]
        assert fp1 == fp2
        assert len(fp1) == 64


class TestExperimentLog:
    def test_default_log_resolves(self):
        path = default_log_path()
        assert str(path).endswith("experiment_log.jsonl")

    def test_append_and_local_regenerate(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("LEGALBENCH_EXPERIMENT_LOG", str(tmp_path / "log.jsonl"))
        monkeypatch.setenv("LEGALBENCH_SIBLING_REPO", "/nonexistent")
        record = build_record(task_id="contract_qa", kind="legalbench_binary_answer",
                              experiment_name="m_legalbench_contract_qa_v1",
                              prompt_version="legalbench_contract_qa_v1", model="m",
                              rows=[{"a": 1}], n_requested=1, seed=1,
                              scores={"accuracy": 0.5}, results=[], tokens={})
        path = append_record(record)
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        loaded = json.loads(lines[0])
        assert loaded["scores"]["accuracy"] == 0.5
        assert "timestamp" in loaded
        # regeneration with an absent sibling writes only the local markdown
        touched = regenerate(path)
        assert touched["site_out"] is None
        local_md = Path(touched["log_md"])
        assert local_md.exists()
        assert "legalbench_contract_qa_v1" in local_md.read_text()

    def test_append_is_append_only(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("LEGALBENCH_EXPERIMENT_LOG", str(tmp_path / "log.jsonl"))
        base = {"experiment_name": "r1"}
        append_record(base)
        append_record({"experiment_name": "r2"})
        names = [json.loads(l)["experiment_name"] for l in
                 (tmp_path / "log.jsonl").read_text().splitlines()]
        assert names == ["r1", "r2"]


class TestMockModel:
    def test_deterministic(self):
        m1 = MockLegalBenchModel(("yes", "no"), seed=5)
        m2 = MockLegalBenchModel(("yes", "no"), seed=5)
        assert m1.answer_binary("q", "d") == m2.answer_binary("q", "d")
        assert m1.classify_family("d") == m2.classify_family("d")
        assert m1.usage()["total_tokens"] > 0
