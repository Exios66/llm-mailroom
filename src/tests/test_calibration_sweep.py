"""scripts/run_calibration_sweep.py: the full-information data collector for threshold calibration.

Everything runs on the deterministic mock LLM; nothing here touches the network or a real model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import graph.build_graph as bg
from pipeline.config import get_confidence_thresholds
from schemas.run_record import RunInfo, RunRecord, to_review_record
from scripts import run_calibration_sweep as sw

NODES = ("intake_node", "classify_node", "retry_classify_node", "review_classify_node",
         "extract_node", "retry_extract_node", "judge_verify_node")
GOLD_MARKER = "ZZ-GOLD-MARKER-91"


@pytest.fixture
def ctx(temp_base_dir):
    run = RunInfo(run_id="t", dataset_id="unit", mode="mock", split="unsplit", gt_in_state=False)
    return sw.SweepContext(run=run, base_dir=temp_base_dir, mock=True, thresholds={"confidence": {}})


@pytest.fixture
def mocked():
    """The sweep's own hooks and mock LLM, as ``main`` installs them."""
    with sw.mock_llm(), sw.installed_hooks():
        yield


def _text_doc(doc_id="d1", text=None, gold="contract", **kw) -> sw.Doc:
    body = text or ("This Master Services Agreement is made between Acme Corp and Beta LLC. " * 12)
    return sw.Doc(doc_id=doc_id, filename=f"{doc_id}.txt", text=body, gold_class=gold, **kw)


def _n_extractions(doc_type: str) -> int:
    return 1 + int(get_confidence_thresholds(doc_type).get("retry_max", 2))


# ── every branch is observed ─────────────────────────────────────────────


def test_every_attempt_is_recorded(ctx, mocked):
    rec = sw.sweep_document(_text_doc(), ctx)
    assert rec.error is None
    assert [a.attempt for a in rec.sorter] == [1, 2]
    assert rec.reviewer.ran and rec.reviewer.mode == "llm" and rec.reviewer.reviewer_doc_type == "contract"
    assert [e.attempt for e in rec.extraction] == list(range(1, _n_extractions("contract") + 1))
    assert {e.doc_type for e in rec.extraction} == {"contract"}
    assert rec.judge.ran and rec.judge.on_attempt == 1 and rec.judge.doc_type == "contract"
    assert rec.observed.sorter_attempts == 2 and rec.observed.reviewer and rec.observed.judge
    assert rec.observed.extraction_attempts == _n_extractions("contract")
    assert rec.observed.full_information is True
    assert rec.run.gt_in_state is False and rec.truncated is False
    assert rec.thresholds_in_force == {"confidence": {}}  # ctx value, copied as given
    # metering: the pipeline's own meter (FakeLangChainLLM reports 120/60 tokens per call)
    assert rec.sorter[0].prompt_tokens == 120 and rec.sorter[0].completion_tokens == 60


def test_judge_attempts_all_judges_every_extraction(ctx, mocked):
    ctx.judge_attempts = "all"
    rec = sw.sweep_document(_text_doc(), ctx)
    n = _n_extractions("contract")
    assert [rec.judge.on_attempt] + [j.on_attempt for j in rec.judge_alt] == list(range(1, n + 1))
    assert rec.observed.judge and rec.observed.full_information


def test_distinct_candidate_classes_each_get_extraction_and_judge(ctx, mocked, monkeypatch):
    """A reviewer that disagrees with the sorter makes both classes observable for replay."""
    real = sw._special_mock_content

    def disagreeing_reviewer(text, expect):
        if "CLASSIFY THIS DOCUMENT" in text:
            return json.dumps({"doc_type": "correspondence", "contract_subtype": None, "doc_subclass": "letter",
                               "confidence": 0.91, "reasoning": "mock"})
        return real(text, expect)

    monkeypatch.setattr(sw, "_special_mock_content", disagreeing_reviewer)
    rec = sw.sweep_document(_text_doc(), ctx)
    by_class = {c: [e.attempt for e in rec.extraction if e.doc_type == c] for c in ("contract", "correspondence")}
    assert by_class["contract"] == list(range(1, _n_extractions("contract") + 1))
    assert by_class["correspondence"] == list(range(1, _n_extractions("correspondence") + 1))
    assert {rec.judge.doc_type, *(j.doc_type for j in rec.judge_alt)} == {"contract", "correspondence"}
    assert [c["source"] for c in rec.diagnostics["extraction_candidates"]] == ["sorter_1", "reviewer"]
    assert rec.reviewer.reviewer_doc_type == "correspondence" and rec.reviewer.reviewer_confidence == 0.91
    assert rec.observed.full_information

    ctx.max_candidates = 1
    capped = sw.sweep_document(_text_doc("d2"), ctx)
    assert capped.diagnostics["extraction_candidates_dropped"] == ["correspondence"]
    assert capped.observed.full_information is False  # a branch the replay may need was not run


def test_raw_confidence_is_kept_beside_the_guarded_one(ctx, mocked, monkeypatch):
    """The sorter's own confidence survives ``apply_classification_guard`` clamping."""
    real = sw.mock_expectation
    monkeypatch.setattr(sw, "mock_expectation", lambda doc: {**real(doc), "subclass": None})
    rec = sw.sweep_document(_text_doc(gold="correspondence"), ctx)
    first = rec.sorter[0]
    assert first.guardrail_issues  # doc_subclass_missing
    assert first.confidence_raw >= 0.95 and first.confidence == 0.5


def test_empty_document_stops_after_one_sorter_call(ctx, mocked, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    rec = sw.sweep_document(sw.Doc(doc_id="e", filename="empty.txt", path=empty, gold_class="contract"), ctx)
    assert rec.error == "empty_or_unreadable_text"
    assert len(rec.sorter) == 1 and not rec.reviewer.ran and rec.extraction == []
    assert rec.observed.full_information is False


# ── gold never reaches the nodes ─────────────────────────────────────────


def test_ground_truth_never_in_node_state(ctx, mocked, monkeypatch):
    seen: list[tuple[str, dict]] = []

    def spy(name):
        real = getattr(bg, name)

        def wrapper(state):
            seen.append((name, dict(state)))
            return real(state)

        return wrapper

    for name in NODES:
        monkeypatch.setattr(bg, name, spy(name))
    doc = _text_doc(
        expected_fields={"parties": [GOLD_MARKER]}, gold_subclass=GOLD_MARKER, expected_stage="archived",
    )
    rec = sw.sweep_document(doc, ctx)
    assert rec.error is None
    assert {n for n, _ in seen} == set(NODES)  # all seven nodes were reached
    for name, state in seen:
        assert not [k for k in sw.GOLD_STATE_KEYS if k in state], name
        assert GOLD_MARKER not in json.dumps(state, default=str), name
    assert rec.gold.expected_doc_subclass == GOLD_MARKER and rec.gold.expected_stage == "archived"


def test_state_with_gold_is_refused_before_the_node_runs(mocked):
    with pytest.raises(RuntimeError, match="gold labels leaked"):
        sw.run_node("classify_node", {"doc_text": "x", "ground_truth": {"expected_doc_class": "contract"}})


def test_initial_state_matches_what_run_pipeline_builds(temp_base_dir, monkeypatch):
    """Drift guard: the sweep's start state equals ``run_pipeline``'s, minus ground truth."""
    captured = {}
    monkeypatch.setattr(bg, "_execute_run", lambda initial_state, **kw: captured.update(initial_state) or {})
    path = Path("some_doc.txt")
    bg.run_pipeline(path, "MATTER-1")
    assert sw.build_initial_state(path, "MATTER-1") == captured


# ── no truncation ────────────────────────────────────────────────────────


def test_long_document_is_never_truncated(ctx, mocked):
    body = "".join(f"Section {i}. The parties agree to the terms of schedule {i}.\n" for i in range(2600))
    assert len(body) > 120_000
    rec = sw.sweep_document(_text_doc(text=body), ctx)
    assert rec.error is None
    assert rec.text_chars == len(body) and rec.truncated is False
    assert rec.diagnostics["sorter_windows"] > 1  # the sorter slid windows over all of it
    assert max(e.n_chunks for e in rec.extraction) > 1  # so did extraction
    assert rec.diagnostics["intake"]["chars"] > 0.9 * len(body)
    # live behaviour the sweep must surface, not hide: these lanes cap their input
    assert rec.diagnostics["downstream_truncation"]["reviewer"] is True
    assert rec.diagnostics["downstream_truncation"]["judge"] is True


# ── offline scoring ──────────────────────────────────────────────────────


def test_extraction_scored_against_gold_only_for_the_gold_class(ctx, mocked, monkeypatch):
    doc = _text_doc(expected_fields={"parties": ["Mock Party"]})
    rec = sw.sweep_document(doc, ctx)
    assert all(isinstance(e.extraction_score, float) and 0.0 <= e.extraction_score <= 1.0 for e in rec.extraction)
    # The model answers "contract" for a gold-correspondence document: scoring a wrong-class
    # extraction against that class's gold fields would be meaningless, so it stays None.
    real = sw.mock_expectation
    monkeypatch.setattr(sw, "mock_expectation", lambda d: real(sw.Doc(d.doc_id, d.filename, gold_class="contract")))
    wrong = sw.sweep_document(_text_doc("d2", gold="correspondence", expected_fields={"parties": ["Mock Party"]}), ctx)
    assert wrong.error is None and {e.doc_type for e in wrong.extraction} == {"contract"}
    assert all(e.extraction_score is None for e in wrong.extraction)
    assert wrong.diagnostics["unscored_class_mismatch"]


# ── CLI: output, resume, failure isolation, real guard ───────────────────


def _run(argv, capsys):
    code = sw.main(argv)
    return code, capsys.readouterr()


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_cli_writes_validating_records(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "2", "--out", str(out)], capsys)
    assert code == 0
    rows = out.read_text().splitlines()
    assert len(rows) == 2
    for line in rows:
        rec = RunRecord.model_validate_json(line)
        assert rec.run.mode == "mock" and rec.run.gt_in_state is False and rec.run.taxonomy_sha256
        assert rec.observed.full_information
        assert to_review_record(rec)["doc_id"] == rec.doc_id
    summary = json.loads(cap.out.split("SUMMARY ", 1)[1])
    assert summary["docs"] == 2 and summary["errors"] == 0 and summary["full_information_share"] == 1.0


def test_resume_skips_documents_already_in_the_output(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "2", "--out", str(out)], capsys)[0] == 0
    first = out.read_text()
    first_ids = [r["doc_id"] for r in _lines(out)]
    # appending to an existing file without --resume is refused (no silent duplicates)
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "3", "--out", str(out)], capsys)[0] == 2
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "3", "--resume", "--out", str(out)], capsys)
    assert code == 0 and "1 document(s) (2 already done)" in cap.out
    rows = _lines(out)
    ids = [r["doc_id"] for r in rows]
    assert out.read_text().startswith(first) and len(ids) == 3 and len(set(ids)) == 3
    assert ids[:2] == first_ids
    assert len({r["run"]["run_id"] for r in rows}) == 1  # one run id across the resume


def test_resume_tolerates_a_torn_last_line_and_retries_errors(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "2", "--out", str(out)], capsys)[0] == 0
    rows = _lines(out)
    rows[0]["error"] = "RuntimeError: boom"
    out.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]) + "\n" + '{"doc_id": "torn", "run"')
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "2", "--resume", "--retry-errors",
                      "--out", str(out)], capsys)
    assert code == 0 and "unparsable" in cap.err
    fixed = [r for r in _lines(out)]
    assert [r["doc_id"] for r in fixed].count(rows[0]["doc_id"]) == 1
    assert all(not r["error"] for r in fixed)


def test_one_failing_document_is_recorded_and_the_sweep_continues(tmp_path, capsys, temp_base_dir, monkeypatch):
    real = bg.classify_node

    def flaky(state):
        if state.get("original_filename") == "sample_nda.txt":
            raise RuntimeError("provider exploded")
        return real(state)

    monkeypatch.setattr(bg, "classify_node", flaky)
    out = tmp_path / "rec.jsonl"
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--out", str(out)], capsys)
    assert code == 1  # non-zero: at least one record carries an error
    rows = {r["doc_id"]: r for r in _lines(out)}
    assert len(rows) == 9
    bad = rows["contract-sample_nda"]
    assert "provider exploded" in bad["error"] and bad["observed"]["full_information"] is False
    assert sum(1 for r in rows.values() if not r["error"]) == 8
    assert json.loads(cap.out.split("SUMMARY ", 1)[1])["errors"] == 1


def test_concurrency_gives_the_same_records_as_serial(tmp_path, capsys, temp_base_dir):
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ("latency_s", "run_id", "diagnostics")}
        return [strip(x) for x in o] if isinstance(o, list) else o

    serial, pooled = tmp_path / "s.jsonl", tmp_path / "p.jsonl"
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "4", "--out", str(serial)], capsys)[0] == 0
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--limit", "4", "--concurrency", "3",
                 "--out", str(pooled)], capsys)[0] == 0
    a = {r["doc_id"]: strip(r) for r in _lines(serial)}
    b = {r["doc_id"]: strip(r) for r in _lines(pooled)}
    assert a == b and len(a) == 4


def test_real_refuses_without_the_env_var_and_makes_no_llm_call(tmp_path, capsys, monkeypatch, temp_base_dir):
    monkeypatch.delenv(sw.ALLOW_REAL_ENV, raising=False)
    monkeypatch.setattr("llm.client.get_llm", lambda *a, **k: pytest.fail("a real client was requested"))
    out = tmp_path / "rec.jsonl"
    code, cap = _run(["--dataset", "mailroom-fixtures", "--real", "--out", str(out)], capsys)
    assert code == 2 and not out.exists()
    assert sw.ALLOW_REAL_ENV in cap.err and "estimated LLM calls" in cap.err


def test_real_refuses_without_a_real_api_key(tmp_path, capsys, monkeypatch, temp_base_dir):
    monkeypatch.setenv(sw.ALLOW_REAL_ENV, "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setattr("llm.client.get_llm", lambda *a, **k: pytest.fail("a real client was requested"))
    code, cap = _run(["--dataset", "mailroom-fixtures", "--real", "--out", str(tmp_path / "r.jsonl")], capsys)
    assert code == 2 and "OPENROUTER_API_KEY" in cap.err


def test_estimate_calls_counts_every_lane():
    est = sw.estimate_calls(10, retry_max=2, candidates=2)
    # 2 sorter + 1 reviewer + 2 classes x (3 extractions + 2 judge calls)
    assert est == {"per_doc": 13, "docs": 10, "total": 130}
    assert sw.estimate_calls(1, retry_max=2, candidates=2, judge_attempts="all")["per_doc"] == 3 + 2 * (3 + 6)


def test_sweep_environment_is_restored_and_never_traces(monkeypatch, tmp_path):
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "langfuse")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    with sw.sweep_environment(True, None) as base:
        import os

        assert os.environ["OBSERVABILITY_PROVIDER"] == "none" and base == tmp_path
    import os

    assert os.environ["OBSERVABILITY_PROVIDER"] == "langfuse"


# ── loaders ──────────────────────────────────────────────────────────────


def _hf_frame():
    import numpy as np
    import pandas as pd

    rows = []
    for cls, sub in (("contract", "license"), ("correspondence", "letter")):
        for i in range(4):
            rows.append({
                "filename": f"{cls}_{i}.txt",
                "doc_text": f"{cls} document {i}. " + "Lorem ipsum dolor sit amet. " * 40,
                "expected": cls,
                "expected_subclass": sub,
                "content_sha256": f"sha-{cls}-{i}",
                "expected_stage": float("nan"),  # a parquet null
                "cuad_clause_labels": np.array(["Governing Law: New York"]) if cls == "contract" else None,
            })
    return pd.DataFrame(rows)


def test_hf_docs_are_a_seeded_per_class_sample_of_the_pinned_loader(monkeypatch):
    calls = []

    def fake_load_corpus(**kw):
        calls.append(kw)
        return _hf_frame(), {"revision_requested": "v9.1", "hub_sha_tip": "abc"}

    monkeypatch.setattr("pipeline.hf_corpus_loader.load_corpus", fake_load_corpus)
    docs, ds = sw.load_hf_docs("test", per_class=2, seed=7)
    assert calls == [{"split": "test"}]
    assert ds == {"dataset_id": "Lucius-Morningstar/mailroom-dataset", "dataset_revision": "v9.1", "split": "test"}
    assert sorted(d.gold_class for d in docs) == ["contract"] * 2 + ["correspondence"] * 2
    frame = _hf_frame().set_index("filename")
    for d in docs:  # full text, byte for byte
        assert d.text == frame.loc[d.filename, "doc_text"] and d.content_sha256.startswith("sha-")
    assert all(d.expected_stage is None for d in docs)  # NaN became None, not the string "nan"
    again, _ = sw.load_hf_docs("test", per_class=2, seed=7)
    assert [d.doc_id for d in again] == [d.doc_id for d in docs]
    other, _ = sw.load_hf_docs("test", per_class=2, seed=8)
    assert [d.doc_id for d in other] != [d.doc_id for d in docs] or len(frame) == 8


def test_manifest_docs_resolve_files_and_gold(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.txt").write_text("hello")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        'id,subdir,filename,expected_doc_class,expected_stage,expected_fields\n'
        'a,s,a.txt,contract,archived,"{""parties"": [""X""]}"\n'
    )
    docs, ds = sw.load_manifest_docs(manifest, None)
    assert docs[0].path == tmp_path / "s" / "a.txt" and docs[0].gold_class == "contract"
    assert docs[0].expected_fields == {"parties": ["X"]} and docs[0].expected_stage == "archived"
    assert ds["dataset_id"] == "manifest:manifest.csv"


def test_hf_split_end_to_end_keeps_full_text_and_provenance(tmp_path, capsys, temp_base_dir, monkeypatch):
    monkeypatch.setattr("pipeline.hf_corpus_loader.load_corpus",
                        lambda **kw: (_hf_frame(), {"revision_requested": "v9.1"}))
    out = tmp_path / "hf.jsonl"
    code, _cap = _run(["--hf-split", "train", "--per-class", "1", "--seed", "3", "--mock", "--out", str(out)], capsys)
    assert code == 0
    rows = [RunRecord.model_validate(r) for r in _lines(out)]
    assert {r.gold.expected_doc_type for r in rows} == {"contract", "correspondence"}
    frame = _hf_frame().set_index("filename")
    for r in rows:
        assert r.run.dataset_revision == "v9.1" and r.run.split == "train" and r.run.mode == "mock"
        assert r.text_chars == len(frame.loc[r.doc_id, "doc_text"]) and r.truncated is False
        assert r.content_sha256 == frame.loc[r.doc_id, "content_sha256"]
        assert r.gold.expected_doc_subclass in ("license", "letter")
        assert r.observed.full_information
