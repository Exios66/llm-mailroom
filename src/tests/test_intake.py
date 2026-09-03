"""Intake clerk — deterministic normalize + ingest span contract."""

from agents.intake import (
    apply_intake,
    deterministic_normalize,
    intake_span_output,
    looks_messy,
)


def test_normalize_collapses_and_unwraps():
    cleaned, stats = deterministic_normalize("A\u00a0B\n\n\n\nagree-\nment")
    assert "A B" in cleaned
    assert "agreement" in cleaned
    assert stats["changed"] is True
    assert stats["hyphen_unwraps"] >= 1
    assert looks_messy("x\n" * 30) is True
    clean, st = deterministic_normalize("Hello world.\n\n1. Clause.")
    assert looks_messy(clean, st) is False


def test_empty_text_is_not_messy():
    cleaned, stats = deterministic_normalize("")
    assert cleaned == ""
    assert stats["changed"] is False
    assert looks_messy(cleaned, stats) is False


def test_intake_span_output_has_eval_keys():
    cleaned, stats = deterministic_normalize("A\n\n\n\nB")
    payload = intake_span_output(stats, looks_messy(cleaned, stats))
    for key in (
        "messy", "changed", "collapsed_blank_runs", "hyphen_unwraps",
        "method", "chars", "raw_chars", "cleaned_chars",
    ):
        assert key in payload
    assert payload["method"] == "deterministic"


def test_ingest_applies_intake(temp_base_dir):
    from graph.build_graph import intake_node, _ensure_dirs
    from graph.state import DocumentState

    _ensure_dirs()
    inbox = temp_base_dir / "pipeline" / "inbox"
    test_file = inbox / "intake_hyphen.txt"
    test_file.write_text("agree-\nment\n\n\n\nNext paragraph.")
    state: DocumentState = {
        "doc_id": "",
        "matter_id": "TEST",
        "original_filename": "intake_hyphen.txt",
        "stage": "inbox",
        "file_path": str(test_file),
        "doc_text": "",
        "messages": [],
    }
    result = intake_node(state)
    assert "agreement" in result["doc_text"]
    assert "\n\n\n" not in result["doc_text"]
    assert result["intake_changed"] is True
    assert "intake_messy" in result


def test_apply_intake_noops_without_langfuse(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    cleaned, stats = apply_intake("Hello   world.\n", filename="x.txt")
    assert cleaned == "Hello world."
    assert stats["method"] == "deterministic"
    assert stats["changed"] is True


def test_mailroom_normalize_matches_dojo_clerk():
    from llm_dojo_scoring.intake import (
        INTAKE_SPAN as DOJO_SPAN,
        deterministic_normalize as dojo_normalize,
    )
    from agents.intake import INTAKE_SPAN

    raw = "A\u00a0B\n\n\n\nagree-\nment"
    ours, our_stats = deterministic_normalize(raw)
    theirs, their_stats = dojo_normalize(raw)
    assert ours == theirs
    assert our_stats == their_stats
    assert INTAKE_SPAN == DOJO_SPAN == "normalize-intake"


def test_intake_suite_scores_prep_completeness():
    from llm_dojo_scoring import get_suite
    from observability.scores import SCORE_CONFIGS
    from observability.suite_scoring import INTAKE_SCORE_NAMES, score_intake_suite

    raw = "A\u00a0B\n\n\n\nagree-\nment"
    cleaned, stats = deterministic_normalize(raw)
    extras = score_intake_suite(raw, cleaned, stats)
    assert extras["intake_prep_completeness"] == 1.0
    assert extras["intake_changed_rate"] == 1.0
    assert extras["intake_hyphen_unwraps"] >= 1.0
    suite_out = get_suite("intake").score(raw, cleaned)
    assert suite_out["intake_prep_completeness"] == 1.0
    names = {c["name"] for c in SCORE_CONFIGS}
    assert INTAKE_SCORE_NAMES <= names
