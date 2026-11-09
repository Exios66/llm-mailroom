import os

import pytest


class TestValidateExtraction:
    def test_parse_error_flag(self):
        from observability.scores import validate_extraction

        result = validate_extraction("contract", {"_parse_error": True})
        assert result["parse_error"] is True
        assert result["schema_valid"] is False

    def test_valid_extraction(self):
        from observability.scores import validate_extraction

        result = validate_extraction(
            "contract",
            {
                "parties": ["ACME", "Zenith"],
                "effective_date": "2024-01-15",
                "term_length": "3 years",
                "cuad_clauses": ["uptime"],
                "governing_law": "Delaware",
                "contract_value": None,
                "renewal_terms": None,
            },
        )
        assert result["parse_error"] is False
        assert result["schema_valid"] is True

    def test_invalid_type_marks_schema_invalid(self):
        from observability.scores import validate_extraction

        # parties must be a list; a string should fail validation
        result = validate_extraction("contract", {"parties": "ACME"})
        assert result["schema_valid"] is False

    def test_unknown_doc_type_is_not_schema_valid(self):
        from observability.scores import validate_extraction

        result = validate_extraction("not_a_real_type", {"foo": 1})
        assert result["schema_valid"] is False
        result_unknown = validate_extraction("unknown", {"_unsupported": True})
        assert result_unknown["schema_valid"] is False

    def test_merger_agreement_validates_against_dedicated_schema(self):
        from observability.scores import validate_extraction

        result = validate_extraction(
            "merger_agreement",
            {
                "parties": ["Parent Inc.", "Target Corp."],
                "effective_date": "2024-06-01",
                "governing_law": "Delaware",
                "merger_consideration": "all_cash",
            },
        )
        assert result["parse_error"] is False
        assert result["schema_valid"] is True


class TestEmitPipelineScores:
    def test_scores_computed_when_tracing_disabled(self):
        # Scores are ALWAYS computed (persisted to the catalog even without a
        # tracing backend); only trace attachment is backend-gated.
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            from observability.scores import emit_pipeline_scores

            scores = emit_pipeline_scores(
                {
                    "doc_id": "d1",
                    "stage": "archived",
                    "doc_type": "contract",
                    "classification_confidence": 0.9,
                    "extracted_data": {"parties": ["A"]},
                },
                metrics={
                    "run_aborted": 0,
                    "run_duration_seconds": 1.5,
                    "total_tokens": 42,
                    "llm_call_count": 3,
                    "estimated_cost_usd": 0.001,
                    "classification_attempts": 1,
                    "extraction_attempts": 1,
                },
            )
            assert scores["stage_completed"] == 1
            assert scores["parse_error"] == 0
            assert scores["schema_valid"] == 1
            assert scores["classification_confidence"] == 0.9
            assert scores["run_aborted"] == 0
            assert scores["total_tokens"] == 42
            assert scores["llm_call_count"] == 3
            assert scores["estimated_cost_usd"] == 0.001
            assert scores["success_rate"] == 1
            assert "class_correct" not in scores
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)


def test_langfuse_score_name_aliases_overlong_verified_precision():
    from observability.scores import langfuse_score_name

    assert langfuse_score_name("extraction_overall_verified_precision") == (
        "extraction_verified_precision"
    )
    assert langfuse_score_name("run_duration_seconds") == "run_duration_seconds"
    assert len("extraction_verified_precision") <= 35


def test_emit_pipeline_scores_class_correct_is_exact_not_aligned():
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
    try:
        from observability.scores import emit_pipeline_scores

        hit = emit_pipeline_scores({
            "stage": "archived",
            "doc_type": "merger_agreement",
            "ground_truth": {"expected_hf_class": "merger_agreement"},
            "extracted_data": {"parties": ["A"]},
        })
        assert hit["class_correct"] == 1
        miss = emit_pipeline_scores({
            "stage": "archived",
            "doc_type": "contract",
            "ground_truth": {
                "expected_doc_class": "merger_agreement",
                "expected_hf_class": "merger_agreement",
            },
            "extracted_data": {"parties": ["A"]},
        })
        assert miss["class_correct"] == 0
    finally:
        os.environ.pop("OBSERVABILITY_PROVIDER", None)


class TestFirstPassSuccess:
    """Production STP: no ground truth, no reroute/reprocess."""

    def test_clean_archive_is_first_pass(self):
        from observability.scores import first_pass_success

        assert first_pass_success(
            {
                "stage": "archived",
                "classification_attempts": 1,
                "extraction_attempts": 1,
                "judge_verdict": "skipped",
            },
            {
                "stage_completed": 1,
                "schema_valid": 1,
                "parse_error": 0,
                "guardrail_triggered": 0,
                "run_aborted": 0,
            },
        ) is True

    def test_does_not_require_ground_truth(self):
        from observability.scores import emit_pipeline_scores
        import os

        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            scores = emit_pipeline_scores(
                {
                    "stage": "archived",
                    "doc_type": "contract",
                    "classification_attempts": 1,
                    "extraction_attempts": 1,
                    "judge_verdict": "skipped",
                    "extracted_data": {
                        "parties": ["ACME", "Zenith"],
                        "effective_date": "2024-01-15",
                        "term_length": "3 years",
                        "cuad_clauses": ["uptime"],
                        "governing_law": "Delaware",
                    },
                }
            )
            assert "class_correct" not in scores
            assert scores["success_rate"] == 1
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)

    def test_retry_is_not_first_pass(self):
        from observability.scores import first_pass_success

        assert first_pass_success(
            {
                "stage": "archived",
                "classification_attempts": 2,
                "extraction_attempts": 1,
            },
            {
                "stage_completed": 1,
                "schema_valid": 1,
                "parse_error": 0,
                "guardrail_triggered": 0,
            },
        ) is False

    def test_lane_a_review_is_not_first_pass(self):
        from observability.scores import first_pass_success

        assert first_pass_success(
            {
                "stage": "archived",
                "classification_attempts": 1,
                "extraction_attempts": 1,
                "review_verdict": "reviewer_agrees",
            },
            {
                "stage_completed": 1,
                "schema_valid": 1,
                "parse_error": 0,
                "guardrail_triggered": 0,
            },
        ) is False

    def test_human_review_and_abort_are_not_first_pass(self):
        from observability.scores import emit_pipeline_scores
        import os

        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            reviewed = emit_pipeline_scores(
                {
                    "stage": "review",
                    "doc_type": "contract",
                    "review_decision": "pending_review",
                    "extracted_data": {"parties": ["A"]},
                },
                metrics={"classification_attempts": 1, "extraction_attempts": 1},
            )
            assert reviewed["success_rate"] == 0
            aborted = emit_pipeline_scores(
                {
                    "stage": "failed",
                    "run_aborted": True,
                    "extracted_data": {},
                },
                metrics={"run_aborted": 1},
            )
            assert aborted["success_rate"] == 0
            assert aborted["stage_completed"] == 0
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)

    def test_guardrail_or_transient_retry_is_not_first_pass(self):
        from observability.scores import first_pass_success

        scores = {
            "stage_completed": 1,
            "schema_valid": 1,
            "parse_error": 0,
            "guardrail_triggered": 1,
        }
        assert first_pass_success(
            {
                "stage": "archived",
                "classification_attempts": 1,
                "extraction_attempts": 1,
                "classification_guardrail": ["unknown class"],
            },
            scores,
        ) is False
        assert first_pass_success(
            {
                "stage": "archived",
                "classification_attempts": 1,
                "extraction_attempts": 1,
                "transient_retries_classify": 1,
            },
            {
                "stage_completed": 1,
                "schema_valid": 1,
                "parse_error": 0,
                "guardrail_triggered": 0,
            },
        ) is False

    def test_gt_class_miss_does_not_define_the_flag(self):
        """Live traffic has no GT. A clean operational path still scores 1
        even when a later pilot label would have been a class miss."""
        from observability.scores import emit_pipeline_scores
        import os

        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            scores = emit_pipeline_scores(
                {
                    "stage": "archived",
                    "doc_type": "contract",
                    "classification_attempts": 1,
                    "extraction_attempts": 1,
                    "judge_verdict": "skipped",
                    "extracted_data": {"parties": ["A"]},
                    "ground_truth": {"expected_doc_class": "correspondence"},
                }
            )
            assert scores["class_correct"] == 0
            assert scores["success_rate"] == 1
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)



class _ScoreClient:
    def __init__(self):
        self.calls = []

    def score_current_trace(self, **kw):
        self.calls.append(("current", kw))

    def create_score(self, **kw):
        self.calls.append(("create", kw))


def test_score_trace_defaults_config_id(monkeypatch):
    from observability import scores

    client = _ScoreClient()
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_config_ids", {"class_correct": "cfg-1"})
    scores.score_trace("class_correct", 1, data_type="BOOLEAN")
    scores.score_trace("class_correct", 1, config_id="explicit")
    scores.score_trace("unknown_metric", 1)
    assert client.calls[0][1]["config_id"] == "cfg-1"
    assert client.calls[1][1]["config_id"] == "explicit"
    assert client.calls[2][1]["config_id"] is None


def test_create_trace_score_defaults_config_id_via_wire_alias(monkeypatch):
    from observability import scores

    client = _ScoreClient()
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_config_ids", {"extraction_verified_precision": "cfg-9"})
    scores.create_trace_score("t1", "extraction_overall_verified_precision", 0.5)
    assert client.calls[0][1]["config_id"] == "cfg-9"


def test_ensure_score_configs_records_ids(monkeypatch):
    from types import SimpleNamespace as NS
    from observability import scores

    created = []

    class Configs:
        def get(self, limit=100):
            return NS(data=[NS(name="class_correct", id="cfg-1")])

        def create(self, **kw):
            created.append(kw["name"])
            return NS(id="new-" + kw["name"])

    client = NS(api=NS(score_configs=Configs()))
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_configs_ensured", set())
    monkeypatch.setattr(scores, "_config_ids", {})
    scores.ensure_score_configs()
    assert scores._config_ids["class_correct"] == "cfg-1"
    assert scores._config_ids["stage_correct"] == "new-stage_correct"
    assert "class_correct" not in created


def test_ensure_score_configs_does_not_cache_after_failure(monkeypatch):
    """#201: a mid-loop create failure must not cache the specs after it."""
    from types import SimpleNamespace as NS
    from observability import scores

    attempts = []

    class Configs:
        def get(self, limit=100):
            return NS(data=[])

        def create(self, **kw):
            attempts.append(kw["name"])
            if len(attempts) == 2:
                raise RuntimeError("boom")
            return NS(id="new-" + kw["name"])

    client = NS(api=NS(score_configs=Configs()))
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_configs_ensured", set())
    monkeypatch.setattr(scores, "_config_ids", {})
    returned = scores.ensure_score_configs()
    # First spec cached, the failed one NOT cached, but the loop moved on and
    # created the specs after the failure.
    first = scores.langfuse_score_name(scores.SCORE_CONFIGS[0]["name"])
    assert first in returned
    failed = scores.langfuse_score_name(scores.SCORE_CONFIGS[1]["name"])
    assert failed not in returned
    assert len(attempts) == len(scores.SCORE_CONFIGS)


def _flaky_config_client(scores, fail_names):
    """Fake Langfuse client: creating a name in ``fail_names`` raises, once."""
    from types import SimpleNamespace as NS

    attempts = []
    failing = set(fail_names)

    class Configs:
        def get(self, limit=100):
            return NS(data=[])

        def create(self, **kw):
            attempts.append(kw["name"])
            if kw["name"] in failing:
                failing.discard(kw["name"])  # fails on the first attempt only
                raise RuntimeError("boom")
            return NS(id="new-" + kw["name"])

    return NS(api=NS(score_configs=Configs())), attempts


def test_ensure_score_configs_retries_only_the_failed_spec(monkeypatch):
    """#201 follow-up: the first creation fails, later ones succeed. The
    non-empty cache used to make every later call return early, so the failed
    config id stayed unavailable for the whole process."""
    from observability import scores

    failed_spec = scores.SCORE_CONFIGS[0]["name"]
    failed_wire = scores.langfuse_score_name(failed_spec)
    client, attempts = _flaky_config_client(scores, {failed_wire})
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_configs_ensured", set())
    monkeypatch.setattr(scores, "_config_ids", {})

    first = scores.ensure_score_configs()
    assert failed_spec not in first
    assert len(first) == len(scores.SCORE_CONFIGS) - 1  # the later specs succeeded
    assert failed_wire not in scores._config_ids
    assert len(attempts) == len(scores.SCORE_CONFIGS)

    second = scores.ensure_score_configs()
    # Only the missing spec was retried; the successful ones were not re-created.
    assert attempts[len(scores.SCORE_CONFIGS):] == [failed_wire]
    assert failed_spec in second
    assert len(second) == len(scores.SCORE_CONFIGS)
    assert scores._config_ids[failed_wire] == "new-" + failed_wire

    # Everything is cached now: a third call makes no Langfuse request.
    scores.ensure_score_configs()
    assert len(attempts) == len(scores.SCORE_CONFIGS) + 1


def test_warmup_retries_a_partially_registered_config_set(monkeypatch):
    from observability import scores

    failed_spec = scores.SCORE_CONFIGS[0]["name"]
    failed_wire = scores.langfuse_score_name(failed_spec)
    client, attempts = _flaky_config_client(scores, {failed_wire})
    monkeypatch.setattr(scores, "_client", lambda: client)
    monkeypatch.setattr(scores, "_configs_ensured", set())
    monkeypatch.setattr(scores, "_config_ids", {})
    monkeypatch.setattr(scores, "_last_warmup_attempt", float("-inf"))

    scores.warmup_score_configs(blocking=True)
    assert failed_spec not in scores._configs_ensured
    # a later warmup (past the sticky backoff) must not be short-circuited by the partial cache
    monkeypatch.setattr(scores, "_last_warmup_attempt", float("-inf"))
    scores.warmup_score_configs(blocking=True)
    assert attempts[len(scores.SCORE_CONFIGS):] == [failed_wire]
    assert failed_spec in scores._configs_ensured


_WIRING_PROBE = """
import importlib, importlib.abc, json, sys, traceback

first_importer = []


class _Recorder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "observability.scoring_wiring" and not first_importer:
            frames = [f.filename for f in traceback.extract_stack() if not f.filename.startswith("<frozen")]
            first_importer.append(frames[-2])  # frames[-1] is this finder
        return None


sys.meta_path.insert(0, _Recorder())
mode = sys.argv[1]
if mode == "wire":  # reference: what the wiring is supposed to produce
    from pipeline.config import load_config
    from llm_dojo_scoring import configure_from_taxonomy

    configure_from_taxonomy(load_config())
elif mode != "none":
    importlib.import_module(mode)  # an entrypoint, with NO separate wiring here

from llm_dojo_scoring import get_settings, get_type_bands

print(json.dumps({
    "importer": first_importer[0] if first_importer else None,
    "type_bands": {k: list(v) for k, v in sorted(get_type_bands().items())},
    "embedding_enabled": get_settings().field_scoring.embedding_enabled,
}))
"""


def _probe_scoring_config(mode: str) -> dict:
    """Effective Dojo field-scoring config seen by a FRESH interpreter."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(src), "OBSERVABILITY_PROVIDER": "none"}
    proc = subprocess.run(
        [sys.executable, "-c", _WIRING_PROBE, mode],
        capture_output=True, text=True, env=env, cwd=str(src), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_scoring_wiring_probe_distinguishes_calibrated_from_defaults():
    """Guard the probe: without wiring the Dojo runs on package defaults."""
    defaults = _probe_scoring_config("none")
    calibrated = _probe_scoring_config("wire")
    assert defaults["type_bands"] == {} and defaults["embedding_enabled"] is False
    assert calibrated["type_bands"] and calibrated["embedding_enabled"] is True


@pytest.mark.parametrize(
    "entrypoint",
    ["graph.build_graph", "scripts.run_pilot", "scripts.run_hf_pilot"],
)
def test_scoring_entrypoint_import_applies_calibrated_taxonomy(entrypoint):
    """#192 tripwire, behavioural: importing each field-scoring entrypoint in
    isolation (no separate wiring) must leave the Dojo on the calibrated
    taxonomy, and the entrypoint's OWN ``import observability.scoring_wiring``
    must be what wires it. A commented-out import fails both checks (a
    substring check on the source would not)."""
    from pathlib import Path

    expected = _probe_scoring_config("wire")
    seen = _probe_scoring_config(entrypoint)
    assert seen["type_bands"] == expected["type_bands"]
    assert seen["embedding_enabled"] == expected["embedding_enabled"]
    src = Path(__file__).resolve().parent.parent
    own_file = src / (entrypoint.replace(".", "/") + ".py")
    assert seen["importer"] is not None and Path(seen["importer"]) == own_file
