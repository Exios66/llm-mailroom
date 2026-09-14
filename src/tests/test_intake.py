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


class TestIntakePreprocessingEdges:
    """Intake/preprocessing node unit coverage — messy heuristics, LLM gate,
    windowing, and validation (deterministic clerk + intake node fast paths)."""

    def test_messy_short_line_heuristic(self):
        from agents.intake import looks_messy

        # >=20 lines with >55% one/two-word lines and short avg line → OCR mess
        text = "\n".join(["a", "b"] * 15)  # 30 one-word lines
        assert looks_messy(text) is True

    def test_messy_clean_paragraph_not_flagged(self):
        from agents.intake import looks_messy

        clean = ("This is a normal paragraph with several words per line.\n" * 5)
        assert looks_messy(clean) is False

    def test_messy_blank_run_collapse_flagged(self):
        from agents.intake import deterministic_normalize, looks_messy

        # 9 separate blank runs (each >=2 newlines) → collapsed_blank_runs >= 8
        raw = "a\n\n\n\nb\n\n\n\nc\n\n\n\nd\n\n\n\ne\n\n\n\nf\n\n\n\ng\n\n\n\nh\n\n\n\ni\n\n\n\nj\n\n\n\nk"
        cleaned, stats = deterministic_normalize(raw)
        assert stats["collapsed_blank_runs"] >= 8
        assert looks_messy(cleaned, stats) is True

    def test_normalize_preserves_table_pipes(self):
        from agents.intake import deterministic_normalize

        raw = "| A | B |\n| 1 | 2 |\n"
        cleaned, _ = deterministic_normalize(raw)
        assert "|" in cleaned  # pipe-delimited rows survive

    def test_normalize_strips_cr_and_nbsp(self):
        from agents.intake import deterministic_normalize

        cleaned, stats = deterministic_normalize("a\r\nb\u00a0c")
        assert "\r" not in cleaned
        assert "\u00a0" not in cleaned
        assert stats["changed"] is True

    def test_should_llm_intake_gate(self):
        from agents.intake import should_llm_intake

        # clean short text → deterministic only (no LLM)
        assert should_llm_intake("clean short text", {"messy": False}) is False
        # messy → LLM intake
        assert should_llm_intake("messy text", {"messy": True}) is True
        # empty → never
        assert should_llm_intake("", {}) is False

    def test_sliding_windows_covers_every_character(self):
        from agents.intake import sliding_windows

        text = "".join(chr(65 + i % 26) for i in range(500))
        windows = sliding_windows(text, budget=200, overlap_chars=50)
        # every character appears in at least one window
        covered = set()
        for chunk, offset in windows:
            covered.update(range(offset, offset + len(chunk)))
        assert covered >= set(range(500))
        # own content never exceeds the budget: window length is budget +
        # the re-sent overlap prefix (<= overlap_chars) + the "\n\n" joiner.
        for chunk, _ in windows:
            assert len(chunk) <= 200 + 50 + 2

    def test_validate_intake_clamps_triage_vocabulary(self):
        from agents.intake import validate_intake

        # an unknown triage token is clamped, not passed through
        result = validate_intake({"triage": {"label": "bogus_lane"}}, "text")
        assert "triage" in result

    def test_apply_intake_stats_include_span_keys(self, monkeypatch):
        from agents.intake import apply_intake

        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        cleaned, stats = apply_intake("A\u00a0B\n\n\n\nagree-\nment", filename="x.txt")
        for key in (
            "messy", "changed", "collapsed_blank_runs", "hyphen_unwraps",
            "method", "chars", "raw_chars", "cleaned_chars",
        ):
            assert key in stats

    def test_intake_node_messy_fast_path(self, temp_base_dir):
        """The intake node marks messy docs without the LLM pass
        (MAILROOM_LLM_INTAKE=0 in conftest) — deterministic clerk only."""
        from graph.build_graph import intake_node, _ensure_dirs

        _ensure_dirs()
        inbox = temp_base_dir / "pipeline" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        messy_file = inbox / "messy.txt"
        # >=20 one-word lines → the short-line heuristic fires
        messy_file.write_text("\n".join(["a", "b"] * 15))
        state = {
            "doc_id": "",
            "matter_id": "TEST",
            "original_filename": "messy.txt",
            "stage": "inbox",
            "file_path": str(messy_file),
            "doc_text": "",
            "messages": [],
        }
        result = intake_node(state)
        assert result["intake_messy"] is True
        assert result["doc_text"]  # clerk still produced cleaned text
