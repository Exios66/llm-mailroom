"""Pluggable decision points: contract, registry, consultation, policy, config, wiring.

Network-free: backends are fakes, the LLM is the conftest mock. The compiled-graph tests
at the bottom run a document through ``build_graph()`` with a slot configured in a copy of
the taxonomy (``decision_points`` fixture).
"""

from __future__ import annotations

import copy
import json
import threading
import time
from types import MappingProxyType

import openai
import pytest
from structlog.testing import capture_logs

import graph.decision_points as dp
from graph import build_graph as bg
from graph.route_spec import ROUTE_SPECS, clear_route_rules, decide_route, route_rules
from pipeline import limits
from pipeline.config import DecisionPointConfigError, get_decision_points_config
from pipeline.reconsideration import collect_review_causes, format_causes

DOC_TEXT = "CONFIDENTIAL text of the matter: Acme pays Beta 1,000,000 USD on 2026-01-01."


@pytest.fixture(autouse=True)
def _clean_registries():
    """Backends, route rules and abandoned threads are process-wide: start and end empty."""
    dp.clear_decision_point_backends()
    clear_route_rules()
    dp._abandoned_threads.clear()
    dp._warned_unregistered.clear()
    dp._warned_missing.clear()
    limits.reset_run_usage()  # compiled-graph runs here bypass run_pipeline, which resets it
    yield
    dp.clear_decision_point_backends()
    clear_route_rules()
    dp._abandoned_threads.clear()
    limits.reset_run_usage()


@pytest.fixture(autouse=True)
def _route_logs_stay_out_of_structlog(monkeypatch):
    """Keep ``route_spec``'s module logger from being first used here.

    structlog binds a module logger on first use (``cache_logger_on_first_use``), and
    ``structlog.testing.capture_logs`` cannot see a logger bound earlier. Running a rule
    through ``decide_route`` logs ``route_rule_applied``; if that were the first log the
    process sees from ``route_spec``, ``test_route_spec.py``'s capture tests would miss
    their events whenever this file runs before it.
    """
    import graph.route_spec as route_spec

    class _Silent:
        def __getattr__(self, level):
            return lambda *a, **k: None

    monkeypatch.setattr(route_spec, "logger", _Silent())


@pytest.fixture
def decision_points(monkeypatch):
    """Set the taxonomy ``decision_points:`` section: ``decision_points({...})``."""
    import pipeline.config as config

    base = copy.deepcopy(config.load_config())

    def apply(section):
        cfg = copy.deepcopy(base)
        cfg["decision_points"] = section
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return apply


class _Recorder:
    def __init__(self):
        self.events = []

    def __getattr__(self, level):
        return lambda event, **kw: self.events.append((level, event, kw))


def policy(**overrides) -> dp.SlotPolicy:
    fields = dict(slot="extract.post", node="extract", backend="fake", allow=("human_review",))
    fields.update(overrides)
    return dp.SlotPolicy(**fields)


def inp(**state) -> dp.DecisionInput:
    state = {"doc_id": "d-1", "doc_text": DOC_TEXT, "doc_type": "contract", **state}
    return dp._build_input(policy(), state)


def ok(**fields) -> dp.DecisionOutcome:
    return dp.DecisionOutcome(**fields)


PARK = {"route_hint": "human_review", "causes": ["fee_mismatch"], "confidence": 0.9, "label": "bad"}


# ---------------------------------------------------------------------------
# DecisionOutcome / normalisation
# ---------------------------------------------------------------------------


class TestOutcome:
    def test_defaults_and_json_round_trip(self):
        outcome = ok(route_hint="human_review", label="x", confidence=1, causes=["a", "a", "b:c"],
                     model="m-1", evidence={"n": [1, 2]})
        assert outcome.causes == ("a", "b:c")  # deduplicated, order kept
        assert outcome.confidence == 1.0 and isinstance(outcome.confidence, float)
        record = outcome.to_dict()
        assert json.loads(json.dumps(record, allow_nan=False)) == record
        assert record["status"] == "ok" and record["causes"] == ["a", "b:c"]

    def test_frozen(self):
        with pytest.raises(Exception):
            ok().label = "x"  # type: ignore[misc]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.01, True, "0.5", 10**400])
    def test_confidence_must_be_finite_unit_float(self, bad):
        with pytest.raises(ValueError):
            ok(confidence=bad)

    @pytest.mark.parametrize(
        "fields",
        [
            {"route_hint": 3},
            {"route_hint": ""},
            {"label": "x" * 201},
            {"causes": "fee_mismatch"},  # a bare string is not a sequence of tokens
            {"causes": ["has space"]},
            {"causes": [f"c{i}" for i in range(9)]},
            {"causes": [1]},
            {"model": 4},
            {"evidence": [1]},
            {"evidence": {"k": {1, 2}}},
            {"evidence": {"k": float("nan")}},
            {"evidence": {"k": "x" * 9000}},
            {"status": "maybe"},
        ],
    )
    def test_malformed_fields_raise(self, fields):
        with pytest.raises(ValueError):
            ok(**fields)

    def test_normalize_accepts_outcome_and_mapping(self):
        outcome = ok(label="x")
        assert dp.normalize_outcome(outcome) is outcome
        got = dp.normalize_outcome({**PARK, "extra": "dropped"})
        assert got.status == "ok" and got.route_hint == "human_review" and got.causes == ("fee_mismatch",)
        assert dp.normalize_outcome({}).status == "ok"

    @pytest.mark.parametrize(
        "raw",
        [None, "human_review", 3, [("route_hint", "x")], {"confidence": 7}, {"causes": "x y"},
         {"evidence": {"k": object()}}, {"status": "weird"}],
    )
    def test_anything_malformed_is_invalid_not_an_exception(self, raw):
        got = dp.normalize_outcome(raw)
        assert got.status == "invalid" and got.route_hint is None
        assert got.evidence["reason"] == "malformed_answer"

    def test_a_value_error_from_a_backend_object_is_recorded_by_class_only(self):
        class Lazy(dict):
            def __getitem__(self, key):
                raise ValueError("Acme owes Beta 1,000,000")

        got = dp.normalize_outcome(Lazy(label="x"))
        assert got.status == "invalid" and got.evidence["detail"] == "ValueError"
        assert "Acme" not in json.dumps(got.to_dict())
        own = dp.normalize_outcome({"confidence": 7})
        assert own.evidence["detail"].startswith("confidence must be")
        with pytest.raises(ValueError):  # still a ValueError for existing callers
            dp.DecisionOutcome(confidence=7)

    def test_a_backend_raising_our_validation_error_is_still_recorded_by_class_only(self):
        class Lazy(dict):
            def __getitem__(self, key):
                raise dp.OutcomeValidationError("Acme owes Beta 1,000,000")

        got = dp.normalize_outcome(Lazy(label="x"))
        assert got.evidence["detail"] == "OutcomeValidationError"
        assert "Acme" not in json.dumps(got.to_dict())

    def test_every_validation_message_is_known(self):
        import inspect
        import re

        raised = set(re.findall(r'raise OutcomeValidationError\("([^"]*)"\)', inspect.getsource(dp)))
        assert raised and raised == set(dp._VALIDATION_MESSAGES)

    def test_invalid_detail_never_echoes_backend_values(self):
        got = dp.normalize_outcome({"label": "SECRET-" + "x" * 300})
        assert "SECRET" not in json.dumps(got.to_dict())


class TestInput:
    def test_is_read_only_and_repr_has_no_document(self):
        i = inp(extraction_confidence=0.9, extracted_data={"parties": ["Acme"], "_raw": "x"})
        assert i.doc_text == DOC_TEXT  # full text, never truncated
        with pytest.raises(TypeError):
            i.state["doc_type"] = "other"  # type: ignore[index]
        with pytest.raises(TypeError):
            i.handoff["x"] = 1  # type: ignore[index]
        assert "CONFIDENTIAL" not in repr(i) and "Acme" not in repr(i)
        assert i.handoff["extraction_confidence"] == 0.9
        assert i.handoff["extraction"]["fields"] == {"parties": ["Acme"]}  # underscore keys dropped

    def test_backends_cannot_mutate_the_live_state(self):
        live = {"parties": ["Acme"], "nested": {"k": [1]}}
        state = {"doc_id": "d-1", "doc_text": DOC_TEXT, "doc_type": "contract", "extracted_data": live,
                 "decision_outcomes": {"other.slot": {"causes": ["x"]}}}
        i = dp._build_input(policy(), state)
        i.state["extracted_data"]["parties"].append("EVIL")
        i.state["extracted_data"]["nested"]["k"].append(2)
        i.state["decision_outcomes"]["other.slot"]["causes"].append("EVIL")
        i.handoff["extraction"]["fields"]["parties"].append("EVIL")
        assert live == {"parties": ["Acme"], "nested": {"k": [1]}}
        assert state["decision_outcomes"] == {"other.slot": {"causes": ["x"]}}

    def test_a_backend_mutating_its_input_does_not_touch_the_node_update(self):
        def meddle(i):
            i.state["extracted_data"]["parties"].append("EVIL")
            return ok()

        dp.register_decision_point_backend("fake", meddle)
        data = {"parties": ["Acme"]}
        out = dp.wrap_node("extract", node_returning({"extracted_data": data}), [policy()])({"doc_type": "contract"})
        assert out["extracted_data"] == {"parties": ["Acme"]}

    def test_a_value_that_cannot_be_copied_is_left_out_not_shared(self):
        import threading as th

        i = dp._build_input(policy(), {"doc_text": "x", "lock": th.Lock(), "doc_type": "contract"})
        assert "lock" not in i.state and i.state["doc_type"] == "contract"

    def test_long_text_is_not_truncated(self):
        text = "x" * 500_000
        assert inp(doc_text=text).doc_text == text

    def test_subclass_falls_back_to_contract_subtype(self):
        assert inp(contract_subtype="nda").doc_subclass == "nda"
        assert inp(doc_subclass="sorter", contract_subtype="nda").doc_subclass == "sorter"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_get_replace_unregister_clear(self):
        a, b = (lambda i: ok()), (lambda i: ok(label="b"))
        assert dp.get_decision_point_backend("x") is None
        dp.register_decision_point_backend("x", a)
        assert dp.get_decision_point_backend("x") is a
        dp.register_decision_point_backend("x", b)
        assert dp.get_decision_point_backend("x") is b
        dp.register_decision_point_backend("y", a)
        assert dp.unregister_decision_point_backend("x") is True
        assert dp.unregister_decision_point_backend("x") is False
        assert dp.get_decision_point_backend("y") is a
        dp.clear_decision_point_backends()
        assert dp.get_decision_point_backend("y") is None

    @pytest.mark.parametrize("name", ["", "  ", None, 3])
    def test_name_must_be_a_non_empty_string(self, name):
        with pytest.raises(ValueError):
            dp.register_decision_point_backend(name, lambda i: ok())

    def test_backend_must_be_callable(self):
        with pytest.raises(TypeError):
            dp.register_decision_point_backend("x", "not callable")  # type: ignore[arg-type]

    def test_concurrent_registration_loses_nothing(self):
        names = [f"b{i}" for i in range(64)]
        threads = [threading.Thread(target=dp.register_decision_point_backend, args=(n, lambda i: ok()))
                   for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(dp.get_decision_point_backend(n) for n in names)

    def test_async_backends_are_refused(self):
        async def coro(inp):
            return ok()

        class AsyncCallable:
            async def __call__(self, inp):
                return ok()

        for backend in (coro, AsyncCallable()):
            with pytest.raises(TypeError, match="synchronous"):
                dp.register_decision_point_backend("a", backend)
        assert dp.get_decision_point_backend("a") is None

    def test_registry_is_separate_from_the_older_one_slot_registries(self):
        from agents.decision_backend import get_decision_backend
        from agents.extraction_gate import get_extraction_gate

        dp.register_decision_point_backend("x", lambda i: ok())
        assert get_decision_backend() is None and get_extraction_gate() is None


# ---------------------------------------------------------------------------
# consult(): bounded, never raises
# ---------------------------------------------------------------------------


class TestConsult:
    def test_ok_answer_is_returned_as_given(self):
        dp.register_decision_point_backend("fake", lambda i: PARK)
        got = dp.consult(policy(), inp())
        assert got.status == "ok" and got.route_hint == "human_review" and got.causes == ("fee_mismatch",)

    def test_backend_receives_the_input(self):
        seen = []
        dp.register_decision_point_backend("fake", lambda i: seen.append(i) or ok())
        dp.consult(policy(), inp(extraction_confidence=0.5))
        assert seen[0].slot == "extract.post" and seen[0].node == "extract"
        assert seen[0].doc_text == DOC_TEXT and seen[0].doc_id == "d-1"

    def test_missing_backend_is_unavailable(self):
        got = dp.consult(policy(), inp())
        assert got.status == "unavailable" and got.evidence["reason"] == "backend_not_registered"

    def test_timeout_is_unavailable_and_the_thread_is_tracked(self):
        release = threading.Event()
        dp.register_decision_point_backend("fake", lambda i: release.wait(10) or ok())
        try:
            started = time.monotonic()
            got = dp.consult(policy(timeout_seconds=0.05), inp())
            assert time.monotonic() - started < 5
            assert got.status == "unavailable" and got.evidence["reason"] == "timeout"
            assert len(dp._abandoned_threads) == 1
        finally:
            release.set()

    def test_default_timeout_is_the_llm_call_timeout(self, monkeypatch):
        monkeypatch.setattr(limits, "get_call_timeout_seconds", lambda: 0.05)
        release = threading.Event()
        dp.register_decision_point_backend("fake", lambda i: release.wait(10) or ok())
        try:
            assert dp.consult(policy(), inp()).evidence["reason"] == "timeout"
        finally:
            release.set()

    def test_slot_timeout_is_capped_by_the_run_deadline(self):
        release = threading.Event()
        dp.register_decision_point_backend("fake", lambda i: release.wait(10) or ok())
        limits.set_run_deadline(time.time() + 0.05)
        try:
            started = time.monotonic()
            got = dp.consult(policy(timeout_seconds=600), inp())
            assert time.monotonic() - started < 5
            assert got.status == "unavailable"
        finally:
            limits.set_run_deadline(None)
            release.set()

    def test_no_time_left_on_the_deadline_never_calls_the_backend(self):
        calls = []
        dp.register_decision_point_backend("fake", lambda i: calls.append(1) or ok())
        limits.set_run_deadline(time.time() - 1)
        try:
            assert dp.consult(policy(), inp()).status == "unavailable"
        finally:
            limits.set_run_deadline(None)
        assert calls == []

    def test_stalled_calls_are_capped_then_pruned(self, monkeypatch):
        monkeypatch.setattr(dp, "_MAX_ABANDONED_DECISION_POINT_THREADS", 2)
        release = threading.Event()
        started = []

        def stalled(i):
            started.append(1)
            release.wait(10)
            return ok()

        dp.register_decision_point_backend("fake", stalled)
        try:
            for _ in range(5):
                assert dp.consult(policy(timeout_seconds=0.05), inp()).status == "unavailable"
            assert len(started) == 2  # the other three were refused, not started
        finally:
            release.set()
        for t in list(dp._abandoned_threads):
            t.join(2)
        dp.register_decision_point_backend("fake", lambda i: ok(label="back"))
        assert dp.consult(policy(), inp()).label == "back"

    def test_decision_point_cap_is_separate_from_the_reviewer_and_gate_caps(self, monkeypatch):
        monkeypatch.setattr(dp, "_MAX_ABANDONED_DECISION_POINT_THREADS", 1)
        release = threading.Event()
        stalled = [threading.Thread(target=release.wait, daemon=True) for _ in range(4)]
        for t in stalled:
            t.start()
        monkeypatch.setattr(bg, "_abandoned_backend_threads", list(stalled))
        monkeypatch.setattr(bg, "_abandoned_gate_threads", list(stalled))
        dp.register_decision_point_backend("fake", lambda i: ok(label="fine"))
        try:
            assert dp.consult(policy(), inp()).label == "fine"
        finally:
            release.set()

    @pytest.mark.parametrize(
        "exc",
        [openai.APIConnectionError(request=None), openai.APITimeoutError(request=None)],
    )
    def test_transient_errors_are_unavailable(self, exc):
        def boom(i):
            raise exc

        dp.register_decision_point_backend("fake", boom)
        got = dp.consult(policy(), inp())
        assert got.status == "unavailable" and got.evidence["reason"] == "transient_error"

    def test_non_transient_errors_are_invalid_and_record_only_the_class(self):
        def boom(i):
            raise RuntimeError("leaked: " + DOC_TEXT)

        dp.register_decision_point_backend("fake", boom)
        with capture_logs() as logs:
            got = dp.consult(policy(), inp())
        assert got.status == "invalid" and got.evidence == {"reason": "backend_error", "error": "RuntimeError"}
        assert "CONFIDENTIAL" not in json.dumps(logs, default=str)

    def test_run_limits_inside_the_backend_do_not_raise_into_the_graph(self):
        def boom(i):
            raise limits.RunDeadlineExceeded(time.time())

        dp.register_decision_point_backend("fake", boom)
        assert dp.consult(policy(), inp()).evidence["reason"] == "run_limit"

    def test_malformed_answer_is_invalid(self):
        dp.register_decision_point_backend("fake", lambda i: {"confidence": 5})
        got = dp.consult(policy(), inp())
        assert got.status == "invalid" and got.route_hint is None

    def test_backend_declared_unavailable_loses_its_hint(self):
        dp.register_decision_point_backend("fake", lambda i: {**PARK, "status": "unavailable"})
        got = dp.consult(policy(), inp())
        assert got.status == "unavailable" and got.route_hint is None  # fail_open: no hint

    def test_fail_open_leaves_no_hint(self):
        got = dp.consult(policy(on_failure="fail_open"), inp())  # no backend registered
        assert got.route_hint is None and got.causes == ()

    @pytest.mark.parametrize(
        "backend, cause",
        [
            (None, "decision_point_unavailable"),
            (lambda i: 1 / 0, "decision_point_invalid"),
            (lambda i: "nonsense", "decision_point_invalid"),
        ],
    )
    def test_fail_safe_parks_with_a_cause(self, backend, cause):
        if backend:
            dp.register_decision_point_backend("fake", backend)
        got = dp.consult(policy(on_failure="fail_safe"), inp())
        assert got.route_hint == "human_review" and got.causes == (cause,) and got.status != "ok"

    def test_consultation_is_one_observation_with_slot_backend_status_and_no_text(self, monkeypatch):
        events = []

        class Span:
            def update(self, **kw):
                events.append(("update", kw))

        class Obs:
            def __init__(self, name, **kw):
                events.append(("open", name, kw))

            def __enter__(self):
                return Span()

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(dp, "observation", Obs)
        dp.register_decision_point_backend("fake", lambda i: PARK)
        dp.consult(policy(), inp())
        opens = [e for e in events if e[0] == "open"]
        assert len(opens) == 1 and opens[0][1] == "consult-decision-point"
        assert opens[0][2]["metadata"]["slot"] == "extract.post"
        assert opens[0][2]["metadata"]["backend"] == "fake"
        update = [e for e in events if e[0] == "update"][0][1]
        assert update["metadata"]["status"] == "ok" and update["output"]["route_hint"] == "human_review"
        assert "CONFIDENTIAL" not in json.dumps(events, default=str)

    def test_the_backend_label_is_never_logged(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        dp.register_decision_point_backend("fake", lambda i: ok(label="Acme owes Beta 1,000,000 USD"))
        dp.consult(policy(), inp())
        (event,) = [e for e in rec.events if e[1] == "decision_point_consulted"]
        assert "label" not in event[2] and event[2]["label_length"] == len("Acme owes Beta 1,000,000 USD")
        assert "Acme" not in json.dumps(rec.events, default=str)

    def test_missing_backend_warns_once_per_slot_under_fail_open(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        for _ in range(3):
            dp.consult(policy(), inp())
        dp.consult(policy(slot="other.slot"), inp())
        warned = [e[2]["slot"] for e in rec.events if e[1] == "decision_point_backend_missing"]
        assert warned == ["extract.post", "other.slot"]
        assert not [e for e in rec.events if e[0] == "warning" and e[1] == "decision_point_consulted"]

    def test_missing_backend_stays_loud_under_fail_safe(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        for _ in range(3):
            dp.consult(policy(on_failure="fail_safe"), inp())
        assert len([e for e in rec.events if e[1] == "decision_point_backend_missing"]) == 3

    def test_a_failing_span_never_fails_the_consultation(self, monkeypatch):
        class Span:
            def update(self, **kw):
                raise RuntimeError("tracing down")

        class Obs:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return Span()

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(dp, "observation", Obs)
        dp.register_decision_point_backend("fake", lambda i: ok(label="x"))
        assert dp.consult(policy(), inp()).label == "x"


# ---------------------------------------------------------------------------
# Policy: vet_outcome and the route rule
# ---------------------------------------------------------------------------


def record(**fields) -> dict:
    return {**ok(**{"route_hint": "human_review", "causes": ["c"], "confidence": 0.9, **fields}).to_dict()}


class TestVet:
    def test_hint_must_be_allowed(self):
        assert dp.vet_outcome(policy(), record()) == "human_review"
        assert dp.vet_outcome(policy(allow=()), record()) is None  # shadow: record, never route
        assert dp.vet_outcome(policy(allow=("compile_report",)), record()) is None
        assert dp.vet_outcome(policy(allow=("compile_report",)), record(route_hint="compile_report")) == "compile_report"

    def test_min_confidence(self):
        p = policy(min_confidence=0.8)
        assert dp.vet_outcome(p, record(confidence=0.8)) == "human_review"
        assert dp.vet_outcome(p, record(confidence=0.79)) is None
        assert dp.vet_outcome(p, record(confidence=None)) is None  # no confidence fails a floor
        assert dp.vet_outcome(policy(), record(confidence=None)) == "human_review"  # no floor

    def test_a_park_needs_causes(self):
        assert dp.vet_outcome(policy(), record(causes=[])) is None
        assert dp.vet_outcome(policy(allow=("compile_report",)), record(route_hint="compile_report", causes=[])) \
            == "compile_report"

    def test_only_ok_outcomes_route_a_model_hint(self):
        for status in ("unavailable", "invalid"):
            assert dp.vet_outcome(policy(), {**record(), "status": status}) is None

    def test_fail_safe_parks_on_failure_whatever_allow_says(self):
        failed = dp._failed_outcome(policy(on_failure="fail_safe"), "unavailable", "timeout").to_dict()
        assert dp.vet_outcome(policy(on_failure="fail_safe", allow=()), failed) == "human_review"
        assert dp.vet_outcome(policy(on_failure="fail_open"), failed) is None
        assert dp.vet_outcome(policy(on_failure="fail_safe"), {**failed, "causes": []}) is None

    def test_by_class_overrides(self):
        p = policy(
            min_confidence=0.5,
            by_class={
                "insurance_claim": dp._ClassOverride(min_confidence=0.95),
                "correspondence": dp._ClassOverride(enabled=False),
                "contract": dp._ClassOverride(allow=("compile_report",)),
            },
        )
        r = record(confidence=0.9)
        assert dp.vet_outcome(p, r, "invoice") == "human_review"          # no entry: slot defaults
        assert dp.vet_outcome(p, r, "insurance_claim") is None            # stricter floor
        assert dp.vet_outcome(p, r, "correspondence") is None             # disabled for the class
        assert dp.vet_outcome(p, r, "contract") is None                   # allow replaced: no park
        assert dp.vet_outcome(p, r, None) == "human_review"

    def test_a_class_can_enable_a_disabled_slot(self):
        p = policy(enabled=False, by_class={"contract": dp._ClassOverride(enabled=True)})
        assert p.active
        assert dp.vet_outcome(p, record(), "contract") == "human_review"
        assert dp.vet_outcome(p, record(), "invoice") is None
        assert not policy(enabled=False).active

    @pytest.mark.parametrize("bad", [None, {}, {"status": "ok"}, {"status": "ok", "route_hint": 5},
                                     {"status": "ok", "route_hint": "human_review", "causes": 3}])
    def test_hostile_records_vet_to_none(self, bad):
        assert dp.vet_outcome(policy(), bad) is None


class TestRule:
    def state(self, **rec):
        return {"doc_type": "contract", "decision_outcomes": {"extract.post": record(**rec)}}

    def test_rule_name_and_override(self):
        rule = dp.rule_for(policy())
        assert rule.name == "decision_point:extract.post"
        assert rule.decide(self.state(), "compile_report") == "human_review"

    def test_agreeing_hint_is_a_no_op(self):
        p = policy(allow=("compile_report",))
        assert dp.rule_for(p).decide(self.state(route_hint="compile_report"), "compile_report") is None

    @pytest.mark.parametrize(
        "proposed", ["human_review", "extract", "retry_extract", "judge_verify", "arbiter", "boss_escalation"]
    )
    def test_never_overrides_a_park_a_self_loop_or_a_verification_gate(self, proposed):
        p = policy(allow=("human_review", "compile_report"))
        assert dp.rule_for(p).decide(self.state(route_hint="compile_report"), proposed) is None

    def test_no_outcome_no_opinion(self):
        rule = dp.rule_for(policy())
        assert rule.decide({}, "compile_report") is None
        assert rule.decide({"decision_outcomes": {}}, "compile_report") is None
        assert rule.decide({"decision_outcomes": {"extract.post": "junk"}}, "compile_report") is None
        assert rule.decide({"decision_outcomes": {"other.slot": record()}}, "compile_report") is None

    def test_rule_is_registered_and_applied_through_decide_route(self):
        spec = next(s for s in ROUTE_SPECS if s.node == "extract")
        dp.install_rules([policy()])
        state = {
            "doc_type": "contract", "doc_text": "x", "extracted_data": {"parties": ["A"]},
            "extraction_confidence": 0.99, "extraction_attempts": 1,
            "decision_outcomes": {"extract.post": record()},
        }
        base = spec.router(dict(state))
        assert base != "human_review"
        assert decide_route(spec, state) == "human_review"
        state["decision_outcomes"] = {}
        assert decide_route(spec, state) == base

    def test_a_hint_cannot_skip_the_lost_window_retry(self):
        """A lost window forces a re-extraction; a ``compile_report`` hint must not skip it."""
        spec = next(s for s in ROUTE_SPECS if s.node == "extract")
        p = policy(allow=("compile_report",))
        dp.install_rules([p])
        state = {
            "doc_type": "contract", "doc_text": "x",
            "extracted_data": {"parties": ["A"], "_failed_chunks": 1},
            "extraction_confidence": 0.99, "extraction_attempts": 1,
            "decision_outcomes": {"extract.post": record(route_hint="compile_report", causes=())},
        }
        assert spec.router(dict(state)) == "retry_extract"
        assert decide_route(spec, state) == "retry_extract"
        assert not dp._rule_answers("extract", "retry_extract", "compile_report")

    @pytest.mark.parametrize(
        "extra, gate",
        [({"extraction_confidence": 0.2, "extraction_attempts": 9}, "judge_verify"),
         ({"conflict_detected": True}, "boss_escalation")],
    )
    def test_a_hint_cannot_skip_the_judge_or_the_conflict_ruling(self, extra, gate):
        spec = next(s for s in ROUTE_SPECS if s.node == "extract")
        dp.install_rules([policy(allow=("compile_report",))])
        state = {
            "doc_type": "contract", "doc_text": "x", "extracted_data": {"parties": ["A"]},
            "extraction_confidence": 0.95, "extraction_attempts": 1,
            "decision_outcomes": {"extract.post": record(route_hint="compile_report", causes=())},
            **extra,
        }
        assert spec.router(dict(state)) == gate
        assert decide_route(spec, state) == gate


class TestRuleInstallation:
    def test_install_is_idempotent_and_replaces_stale_rules(self):
        dp.install_rules([policy(), policy(slot="extract.second")])
        dp.install_rules([policy(), policy(slot="extract.second")])
        assert [r.name for r in route_rules("extract")] == ["decision_point:extract.post",
                                                            "decision_point:extract.second"]
        dp.install_rules([policy(slot="judge_verify.post", node="judge_verify")])
        assert route_rules("extract") == ()
        assert [r.name for r in route_rules("judge_verify")] == ["decision_point:judge_verify.post"]
        dp.install_rules([])
        assert all(route_rules(s.node) == () for s in ROUTE_SPECS)

    def test_other_rules_are_left_alone(self):
        from graph.route_spec import RouteRule, add_route_rule

        add_route_rule("extract", RouteRule("mine", lambda s, p: None))
        dp.install_rules([policy()])
        dp.clear_rules()
        assert [r.name for r in route_rules("extract")] == ["mine"]

    def test_install_never_exposes_an_empty_rule_set_to_a_concurrent_reader(self):
        dp.install_rules([policy()])
        stop = threading.Event()
        errors = []

        def reinstall():
            while not stop.is_set():
                dp.install_rules([policy()])

        writer = threading.Thread(target=reinstall, daemon=True)
        writer.start()
        try:
            for _ in range(20000):
                names = [r.name for r in route_rules("extract")]
                if names != ["decision_point:extract.post"]:
                    errors.append(names)
                    break
        finally:
            stop.set()
            writer.join(5)
        assert errors == []

    def test_replace_validates_before_changing_anything(self):
        from graph.route_spec import RouteRule, replace_route_rules

        dp.install_rules([policy()])
        with pytest.raises(ValueError):
            replace_route_rules(dp.RULE_PREFIX, [("nope", RouteRule("decision_point:x", lambda s, p: None))])
        with pytest.raises(ValueError):
            replace_route_rules(dp.RULE_PREFIX, [("extract", RouteRule("decision_point:x", lambda s, p: None)),
                                                 ("extract", RouteRule("decision_point:x", lambda s, p: None))])
        assert [r.name for r in route_rules("extract")] == ["decision_point:extract.post"]

    def test_reset_compiled_graph_clears_the_rules_under_the_graph_lock(self, monkeypatch):
        held = []
        real = dp.clear_rules
        monkeypatch.setattr(dp, "clear_rules", lambda: (held.append(bg._compiled_graph_lock.locked()), real()))
        dp.install_rules([policy()])
        bg.reset_compiled_graph()
        assert held == [True] and route_rules("extract") == ()

    def test_reset_compiled_graph_clears_the_rules(self):
        dp.install_rules([policy()])
        bg.reset_compiled_graph()
        assert route_rules("extract") == ()


# ---------------------------------------------------------------------------
# Config: loader and graph validation
# ---------------------------------------------------------------------------


GOOD = {
    "extract.post": {
        "node": "extract", "backend": "fake", "timeout_seconds": 5, "on_failure": "fail_safe",
        "allow": ["human_review", "judge_verify"], "min_confidence": 0.8,
        "by_class": {"insurance_claim": {"min_confidence": 0.9, "allow": ["human_review"]},
                     "correspondence": {"enabled": False}},
    }
}


class TestConfig:
    def test_shipped_taxonomy_has_no_decision_points(self):
        assert get_decision_points_config() == {}
        assert dp.prepare_slots() == ()

    @pytest.mark.parametrize("section", [None, {}])
    def test_empty_section_is_no_slot(self, decision_points, section):
        decision_points(section)
        assert get_decision_points_config() == {}

    def test_normalised_with_defaults(self, decision_points):
        decision_points({"classify.post": {"node": "classify", "backend": " b "}})
        assert get_decision_points_config() == {
            "classify.post": {
                "node": "classify", "backend": "b", "enabled": True, "timeout_seconds": None,
                "on_failure": "fail_open", "allow": [], "min_confidence": None, "by_class": {},
                "max_overrides": 1,
            }
        }

    def test_good_config_builds_policies(self, decision_points):
        decision_points(GOOD)
        (p,) = dp.prepare_slots()
        assert (p.slot, p.node, p.backend, p.on_failure, p.timeout_seconds) == (
            "extract.post", "extract", "fake", "fail_safe", 5.0)
        assert p.allow == ("human_review", "judge_verify") and p.min_confidence == 0.8
        assert p.by_class["insurance_claim"].allow == ("human_review",)
        assert p.by_class["correspondence"].enabled is False

    def test_disabled_slots_are_validated_but_not_active(self, decision_points):
        decision_points({"extract.post": {"node": "extract", "backend": "b", "enabled": False}})
        assert dp.prepare_slots() == ()
        decision_points({"extract.post": {"node": "intake", "backend": "b", "enabled": False}})
        with pytest.raises(DecisionPointConfigError):
            dp.prepare_slots()

    @pytest.mark.parametrize(
        "section, needle",
        [
            ([], "must be a mapping"),
            ({"Extract": {"node": "extract", "backend": "b"}}, "slot names"),
            ({"extract.post": []}, "must be a mapping"),
            ({"extract.post": {"node": "extract", "backend": "b", "alow": []}}, "unknown key(s) ['alow']"),
            ({"extract.post": {"backend": "b"}}, "node is required"),
            ({"extract.post": {"node": "extract"}}, "backend is required"),
            ({"extract.post": {"node": "extract", "backend": "b", "enabled": "yes"}}, "enabled must be"),
            ({"extract.post": {"node": "extract", "backend": "b", "timeout_seconds": 0}}, "timeout_seconds"),
            ({"extract.post": {"node": "extract", "backend": "b", "timeout_seconds": True}}, "timeout_seconds"),
            ({"extract.post": {"node": "extract", "backend": "b", "on_failure": "fail_closed"}}, "on_failure"),
            ({"extract.post": {"node": "extract", "backend": "b", "allow": "human_review"}}, "allow must be a list"),
            ({"extract.post": {"node": "extract", "backend": "b", "allow": ["a", "a"]}}, "twice"),
            ({"extract.post": {"node": "extract", "backend": "b", "min_confidence": 1.5}}, "min_confidence"),
            ({"extract.post": {"node": "extract", "backend": "b", "min_confidence": float("nan")}}, "min_confidence"),
            ({"extract.post": {"node": "extract", "backend": "b", "max_overrides": -1}}, "max_overrides must be"),
            ({"extract.post": {"node": "extract", "backend": "b", "max_overrides": True}}, "max_overrides must be"),
            ({"extract.post": {"node": "extract", "backend": "b", "max_overrides": 1.5}}, "max_overrides must be"),
            ({"extract.post": {"node": "extract", "backend": "b", "by_class": {"nonsense": {}}}}, "unknown class"),
            ({"extract.post": {"node": "extract", "backend": "b", "by_class": {"contract": {"x": 1}}}},
             "unknown key(s) ['x']"),
            ({"extract.post": {"node": "extract", "backend": "b", "by_class": {"contract": []}}}, "must be a mapping"),
        ],
    )
    def test_structural_errors_name_the_problem(self, decision_points, section, needle):
        decision_points(section)
        with pytest.raises(DecisionPointConfigError, match="decision_points") as err:
            get_decision_points_config()
        assert needle in str(err.value)

    def test_every_problem_is_reported_at_once(self, decision_points):
        decision_points({"a.b": {"backend": "b", "zzz": 1}, "c.d": {"node": "extract", "on_failure": "no"}})
        with pytest.raises(DecisionPointConfigError) as err:
            get_decision_points_config()
        text = str(err.value)
        assert "a.b: unknown key" in text and "a.b: node is required" in text and "c.d: on_failure" in text

    @pytest.mark.parametrize(
        "slot, needle",
        [
            ({"node": "no_such_node", "backend": "b"}, "has no conditional route"),
            ({"node": "intake", "backend": "b"}, "has no conditional route"),
            ({"node": "archive", "backend": "b"}, "has no conditional route"),
            ({"node": "human_review", "backend": "b"}, "a person's decision is final"),
            ({"node": "__start__", "backend": "b"}, "not a node"),
            ({"node": "extract", "backend": "b", "allow": ["catalog_write"]}, "not a destination of 'extract'"),
            ({"node": "classify", "backend": "b", "allow": ["judge_verify"]}, "not a destination of 'classify'"),
            ({"node": "extract", "backend": "b", "allow": ["human_review", "extract"]}, "the node itself"),
            ({"node": "extract", "backend": "b", "by_class": {"contract": {"allow": ["archive"]}}},
             "by_class.contract.allow lists 'archive'"),
            ({"node": "extract", "backend": "b", "by_class": {"contract": {"allow": ["extract"]}}},
             "the node itself"),
        ],
    )
    def test_graph_validation(self, decision_points, slot, needle):
        decision_points({"x.post": slot})
        with pytest.raises(DecisionPointConfigError) as err:
            dp.prepare_slots()
        assert needle in str(err.value)

    def test_build_graph_fails_fast_on_a_bad_section(self, decision_points):
        decision_points({"extract.post": {"node": "extract", "backend": "b", "allow": ["extract"]}})
        with pytest.raises(DecisionPointConfigError, match="the node itself"):
            bg.build_graph()


# ---------------------------------------------------------------------------
# wrap_node / wrap_human_review
# ---------------------------------------------------------------------------


def node_returning(update):
    def node(state):
        return dict(update)

    return node


class TestWrapNode:
    def test_no_policy_for_the_node_returns_the_function_itself(self):
        fn = node_returning({})
        assert dp.wrap_node("extract", fn, []) is fn
        assert dp.wrap_node("extract", fn, [policy(slot="j.post", node="judge_verify")]) is fn
        assert dp.wrap_human_review(fn, []) is fn

    def test_outcome_is_written_and_merged_with_existing(self):
        dp.register_decision_point_backend("fake", lambda i: PARK)
        wrapped = dp.wrap_node("extract", node_returning({"extraction_confidence": 0.9}), [policy()])
        assert wrapped.mailroom_decision_slots == ("extract.post",)
        out = wrapped({"doc_type": "contract", "doc_text": DOC_TEXT,
                       "decision_outcomes": {"classify.post": {"status": "ok"}}})
        assert out["extraction_confidence"] == 0.9
        assert set(out["decision_outcomes"]) == {"classify.post", "extract.post"}
        stored = out["decision_outcomes"]["extract.post"]
        assert stored["status"] == "ok" and stored["backend"] == "fake"
        assert stored["vetted_route"] == "human_review"
        json.dumps(out["decision_outcomes"], allow_nan=False)

    def test_backend_sees_the_merged_state(self):
        seen = []
        dp.register_decision_point_backend("fake", lambda i: seen.append(i) or ok())
        wrapped = dp.wrap_node("extract", node_returning({"extraction_confidence": 0.42}), [policy()])
        wrapped({"doc_type": "contract", "doc_text": DOC_TEXT, "extraction_confidence": 0.1})
        assert seen[0].handoff["extraction_confidence"] == 0.42

    @pytest.mark.parametrize("on_failure,route", [("fail_open", None), ("fail_safe", "human_review")])
    def test_input_that_cannot_be_built_is_invalid_not_a_crash(self, monkeypatch, on_failure, route):
        calls = []
        dp.register_decision_point_backend("fake", lambda i: calls.append(1) or ok())

        def boom(policy, state):
            raise RuntimeError("cannot copy")

        monkeypatch.setattr(dp, "_build_input", boom)
        wrapped = dp.wrap_node("extract", node_returning({"x": 1}), [policy(on_failure=on_failure)])
        out = wrapped({"doc_type": "contract", "doc_text": DOC_TEXT})
        stored = out["decision_outcomes"]["extract.post"]
        assert calls == [] and out["x"] == 1
        assert stored["status"] == "invalid" and stored["evidence"]["reason"] == "input_build_failed"
        assert stored["evidence"]["error_type"] == "RuntimeError"
        assert stored["vetted_route"] == route

    def test_transient_retry_is_not_consulted_and_drops_the_stale_entry(self):
        calls = []
        dp.register_decision_point_backend("fake", lambda i: calls.append(1) or PARK)
        wrapped = dp.wrap_node("extract", node_returning({"transient_error": True}), [policy()])
        stale = {"extract.post": record(), "classify.post": {"status": "ok"}}
        out = wrapped({"doc_type": "contract", "decision_outcomes": stale})
        assert calls == []
        assert out["decision_outcomes"] == {"classify.post": {"status": "ok"}}

    def test_nothing_consulted_and_nothing_stale_leaves_state_alone(self):
        calls = []
        dp.register_decision_point_backend("fake", lambda i: calls.append(1) or PARK)
        wrapped = dp.wrap_node("extract", node_returning({"transient_error": True, "x": 1}), [policy()])
        assert wrapped({"doc_type": "contract"}) == {"transient_error": True, "x": 1}
        off = dp.wrap_node("extract", node_returning({"x": 1}),
                           [policy(by_class={"contract": dp._ClassOverride(enabled=False)})])
        assert off({"doc_type": "contract"}) == {"x": 1}
        assert calls == []

    def test_a_pass_replaces_the_previous_entry(self):
        answers = iter([PARK, {"label": "second"}])
        dp.register_decision_point_backend("fake", lambda i: next(answers))
        wrapped = dp.wrap_node("extract", node_returning({}), [policy()])
        first = wrapped({"doc_type": "contract"})
        second = wrapped({"doc_type": "contract", **first})
        assert second["decision_outcomes"]["extract.post"]["label"] == "second"
        assert second["decision_outcomes"]["extract.post"]["vetted_route"] is None

    def test_slots_of_one_node_run_in_config_order(self):
        order = []
        dp.register_decision_point_backend("fake", lambda i: order.append(i.slot) or ok())
        wrapped = dp.wrap_node("extract", node_returning({}),
                               [policy(slot="extract.a"), policy(slot="extract.b")])
        out = wrapped({"doc_type": "contract"})
        assert order == ["extract.a", "extract.b"] and list(out["decision_outcomes"]) == order

    def test_failure_is_recorded_not_raised(self):
        dp.register_decision_point_backend("fake", lambda i: 1 / 0)
        wrapped = dp.wrap_node("extract", node_returning({"x": 1}), [policy(on_failure="fail_safe")])
        out = wrapped({"doc_type": "contract"})
        assert out["x"] == 1
        stored = out["decision_outcomes"]["extract.post"]
        assert stored["status"] == "invalid" and stored["vetted_route"] == "human_review"

    def test_the_node_own_errors_and_non_mapping_updates_pass_through(self):
        def boom(state):
            raise ValueError("node failed")

        with pytest.raises(ValueError, match="node failed"):
            dp.wrap_node("extract", boom, [policy()])({})
        marker = object()
        assert dp.wrap_node("extract", lambda s: marker, [policy()])({}) is marker

    def test_human_review_wrapper_clears_outcomes_after_the_person_decides(self):
        wrapper = dp.wrap_human_review(node_returning({"review_decision": "approved"}), [policy()])
        out = wrapper({"decision_outcomes": {"extract.post": record()}})
        assert out == {"review_decision": "approved", "decision_outcomes": {}}
        assert wrapper({}) == {"review_decision": "approved"}  # nothing to clear: state untouched

    def test_human_review_interrupt_passes_through(self):
        from langgraph.errors import GraphInterrupt

        def parks(state):
            raise GraphInterrupt()

        with pytest.raises(GraphInterrupt):
            dp.wrap_human_review(parks, [policy()])({"decision_outcomes": {"x": {}}})


# ---------------------------------------------------------------------------
# Review causes
# ---------------------------------------------------------------------------


class TestReviewCauses:
    def test_parking_outcome_becomes_a_namespaced_cause(self):
        state = {"doc_type": "contract",
                 "decision_outcomes": {"extract.post": {**record(causes=["fee_mismatch", "late"]),
                                                         "vetted_route": "human_review"}}}
        causes = collect_review_causes(state)
        assert "decision_point:extract.post:fee_mismatch" in causes
        assert "decision_point:extract.post:late" in causes
        assert "decision point extract.post: fee mismatch" in format_causes(causes)

    def test_outcomes_that_did_not_ask_to_park_add_nothing(self):
        for route in (None, "compile_report"):
            state = {"decision_outcomes": {"s.post": {**record(), "vetted_route": route}}}
            assert collect_review_causes(state) == []

    @pytest.mark.parametrize("bad", [None, [], "x", {"s.post": "junk"}, {3: {"vetted_route": "human_review"}},
                                     {"s.post": {"vetted_route": "human_review", "causes": "abc"}}])
    def test_hostile_outcomes_are_ignored(self, bad):
        assert collect_review_causes({"decision_outcomes": bad}) == []

    def test_fail_safe_failure_cause(self):
        failed = dp._failed_outcome(policy(on_failure="fail_safe"), "unavailable", "timeout").to_dict()
        failed["vetted_route"] = dp.vet_outcome(policy(on_failure="fail_safe"), failed)
        assert collect_review_causes({"decision_outcomes": {"s.post": failed}}) == [
            "decision_point:s.post:decision_point_unavailable"]


# ---------------------------------------------------------------------------
# Compiled graph, end to end (mock LLM)
# ---------------------------------------------------------------------------

SLOT = {"extract.post": {"node": "extract", "backend": "fake", "allow": ["human_review"]}}


def run_doc(temp_base_dir, mock_langchain_llm, name="doc.txt", thread="dp"):
    mock_langchain_llm.classification = {
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "contract",
    }
    for sub in ("inbox", "processing", "classified", "review", "failed"):
        (temp_base_dir / "pipeline" / sub).mkdir(parents=True, exist_ok=True)
    path = temp_base_dir / "pipeline" / "inbox" / name
    path.write_text("MASTER SERVICES AGREEMENT between Acme Corp. and Beta LLC.")
    graph = bg.build_graph()
    state = {
        "doc_id": "", "matter_id": "TEST-MATTER", "original_filename": name, "stage": "inbox",
        "doc_type": None, "classification_confidence": None, "classification_attempts": 0,
        "extracted_data": None, "extraction_confidence": None, "extraction_attempts": 0,
        "trace_id": None, "escalation_reason": None, "review_decision": None, "retry_count": 0,
        "conflict_detected": False, "file_path": str(path), "doc_text": "", "error_message": None,
        "messages": [],
    }
    return graph.invoke(state, {"configurable": {"thread_id": thread}})


def unwrap_chain(fn):
    while fn is not None:
        yield fn
        fn = getattr(fn, "__wrapped__", None)


def node_functions(graph):
    for name, node in graph.builder.nodes.items():
        runnable = getattr(node, "runnable", None)
        yield name, getattr(runnable, "func", None)


class TestCompiledGraph:
    def test_default_config_installs_nothing(self, temp_base_dir, mock_openai_client, mock_langchain_llm):
        graph = bg.build_graph()
        for name, func in node_functions(graph):
            assert func is not None, name
            assert not any(
                hasattr(f, "mailroom_decision_slots") or hasattr(f, "mailroom_clears_decision_outcomes")
                for f in unwrap_chain(func)
            ), name
        assert all(route_rules(s.node) == () for s in ROUTE_SPECS)

    def test_default_config_run_has_no_decision_outcomes(self, temp_base_dir, mock_openai_client,
                                                          mock_langchain_llm):
        dp.register_decision_point_backend("fake", lambda i: pytest.fail("consulted with no slot"))
        result = run_doc(temp_base_dir, mock_langchain_llm)
        assert result["stage"] == "archived"
        assert "decision_outcomes" not in result

    def test_enabled_slot_wraps_only_its_node_and_registers_its_rule(self, decision_points, temp_base_dir,
                                                                       mock_openai_client, mock_langchain_llm):
        decision_points(SLOT)
        graph = bg.build_graph()
        wrapped = {
            name for name, func in node_functions(graph)
            if any(hasattr(f, "mailroom_decision_slots") for f in unwrap_chain(func))
        }
        cleared = {
            name for name, func in node_functions(graph)
            if any(hasattr(f, "mailroom_clears_decision_outcomes") for f in unwrap_chain(func))
        }
        assert wrapped == {"extract"} and cleared == {"human_review"}
        assert [r.name for r in route_rules("extract")] == ["decision_point:extract.post"]
        bg.build_graph()  # rebuilding is idempotent
        assert [r.name for r in route_rules("extract")] == ["decision_point:extract.post"]

    def test_backend_hint_parks_the_document_with_a_visible_cause(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points(SLOT)
        seen = []

        def backend(i):
            seen.append(i)
            return {"route_hint": "human_review", "label": "fee_mismatch", "confidence": 0.93,
                    "causes": ["fee_mismatch"], "model": "dp-model-1"}

        dp.register_decision_point_backend("fake", backend)
        result = run_doc(temp_base_dir, mock_langchain_llm)

        assert len(seen) == 1 and seen[0].node == "extract" and seen[0].doc_type == "contract"
        assert "Acme Corp." in seen[0].doc_text
        interrupts = result.get("__interrupt__")
        assert interrupts, f"document was not parked: stage={result.get('stage')}"
        payload = interrupts[0].value
        assert "decision_point:extract.post:fee_mismatch" in payload["review_causes"]
        assert result["decision_outcomes"]["extract.post"]["vetted_route"] == "human_review"
        from pipeline.bins import review_dir

        assert list(review_dir().glob("doc--*.txt"))

    def test_without_the_rule_the_same_hint_does_not_park(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        """Control for the test above: the park comes from the installed rule, not the wrapper."""
        decision_points(SLOT)
        dp.register_decision_point_backend(
            "fake", lambda i: {"route_hint": "human_review", "causes": ["x"], "confidence": 0.9})
        original = dp.install_rules
        dp.install_rules = lambda policies: dp.clear_rules()  # type: ignore[assignment]
        try:
            result = run_doc(temp_base_dir, mock_langchain_llm)
        finally:
            dp.install_rules = original  # type: ignore[assignment]
        assert result["stage"] == "archived" and not result.get("__interrupt__")

    def test_hint_outside_allow_or_below_min_confidence_does_not_park(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"extract.post": {**SLOT["extract.post"], "min_confidence": 0.95}})
        dp.register_decision_point_backend(
            "fake", lambda i: {"route_hint": "human_review", "causes": ["x"], "confidence": 0.9})
        result = run_doc(temp_base_dir, mock_langchain_llm)
        assert result["stage"] == "archived"
        stored = result["decision_outcomes"]["extract.post"]
        assert stored["status"] == "ok" and stored["vetted_route"] is None

    def test_backend_error_under_fail_open_leaves_routing_unchanged(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points(SLOT)

        def boom(i):
            raise openai.APIConnectionError(request=None)

        dp.register_decision_point_backend("fake", boom)
        result = run_doc(temp_base_dir, mock_langchain_llm)
        assert result["stage"] == "archived" and not result.get("__interrupt__")
        stored = result["decision_outcomes"]["extract.post"]
        assert stored["status"] == "unavailable" and stored["route_hint"] is None
        assert "review_causes" not in result or not result["review_causes"]

    def test_backend_error_under_fail_safe_parks(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"extract.post": {**SLOT["extract.post"], "on_failure": "fail_safe"}})
        dp.register_decision_point_backend("fake", lambda i: 1 / 0)
        result = run_doc(temp_base_dir, mock_langchain_llm)
        interrupts = result.get("__interrupt__")
        assert interrupts
        assert "decision_point:extract.post:decision_point_invalid" in interrupts[0].value["review_causes"]

    def test_missing_backend_under_fail_open_does_not_stop_the_run(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points(SLOT)
        result = run_doc(temp_base_dir, mock_langchain_llm)
        assert result["stage"] == "archived"
        assert result["decision_outcomes"]["extract.post"]["evidence"]["reason"] == "backend_not_registered"

    def test_a_recorded_only_slot_never_routes(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"extract.post": {"node": "extract", "backend": "fake"}})  # no allow: shadow
        dp.register_decision_point_backend(
            "fake", lambda i: {"route_hint": "human_review", "causes": ["x"], "confidence": 0.9})
        result = run_doc(temp_base_dir, mock_langchain_llm)
        assert result["stage"] == "archived"
        assert result["decision_outcomes"]["extract.post"]["label"] is None

    def test_decision_outcomes_are_not_in_the_node_span_output(self):
        from observability.tracing import _result_summary

        summary = _result_summary({"decision_outcomes": {"s": {"evidence": {"k": "v"}}}, "stage": "x"})
        assert "decision_outcomes" not in summary


# ---------------------------------------------------------------------------
# Override budget (max_overrides)
# ---------------------------------------------------------------------------

PROPOSES_COMPILE = {
    "doc_type": "contract", "doc_text": DOC_TEXT, "extracted_data": {"parties": ["A"]},
    "extraction_confidence": 0.99, "extraction_attempts": 1,
}


def hint_to(dest, **extra):
    return {"route_hint": dest, "confidence": 0.9, **extra}


def redirecting(**overrides):
    return policy(allow=("judge_verify", "human_review"), **overrides)


class TestOverrideBudget:
    def test_default_budget_is_one_and_validated_in_total(self, decision_points):
        decision_points({"extract.post": {"node": "extract", "backend": "b"}})
        assert dp.load_slot_policies()[0].max_overrides == 1
        decision_points({
            f"s{i}.post": {"node": n, "backend": "b", "max_overrides": 3, "allow": ["compile_report"]}
            for i, n in enumerate(["extract", "judge_verify", "retry_extract"])
        })
        with pytest.raises(DecisionPointConfigError, match=r"add up to 9; at most 8") as err:
            dp.prepare_slots()
        assert "max_overrides: 0" in str(err.value)

    def test_record_only_and_park_only_slots_do_not_count_toward_the_total(self, decision_points):
        big = {"backend": "b", "max_overrides": 8}
        decision_points({
            "a.post": {"node": "classify", "allow": ["extract"], **big},           # redirects: counts
            "b.post": {"node": "extract", **big},                                   # record-only
            "c.post": {"node": "judge_verify", "allow": ["human_review"], **big},   # park-only
            "d.post": {"node": "retry_extract", "allow": ["compile_report"], "backend": "b",
                       "max_overrides": 8, "enabled": False},                       # inactive
        })
        assert [p.slot for p in dp.prepare_slots()] == ["a.post", "b.post", "c.post"]
        assert [p.can_redirect for p in dp.load_slot_policies()] == [True, False, False, True]

    def test_a_class_override_can_make_a_slot_redirect(self, decision_points):
        decision_points({"a.post": {"node": "extract", "backend": "b", "max_overrides": 8,
                                    "by_class": {"contract": {"allow": ["compile_report"]}}},
                         "b.post": {"node": "classify", "backend": "b", "max_overrides": 1,
                                    "allow": ["extract"]}})
        with pytest.raises(DecisionPointConfigError, match="add up to 9"):
            dp.prepare_slots()

    def test_inactive_slots_do_not_count_toward_the_total(self, decision_points):
        decision_points({
            "a.post": {"node": "classify", "backend": "b", "max_overrides": 8},
            "b.post": {"node": "extract", "backend": "b", "max_overrides": 8, "enabled": False},
        })
        assert len(dp.prepare_slots()) == 1

    def test_a_redirect_spends_the_budget_and_the_next_one_is_capped(self):
        dp.register_decision_point_backend("fake", lambda i: hint_to("judge_verify"))
        wrapped = dp.wrap_node("extract", node_returning({}), [redirecting(max_overrides=1)])
        first = wrapped(dict(PROPOSES_COMPILE))
        assert first["decision_override_counts"] == {"extract.post": 1}
        assert first["decision_outcomes"]["extract.post"]["vetted_route"] == "judge_verify"
        assert "capped" not in first["decision_outcomes"]["extract.post"]
        second = wrapped({**PROPOSES_COMPILE, **first})
        stored = second["decision_outcomes"]["extract.post"]
        assert stored["capped"] is True and stored["vetted_route"] is None
        assert "decision_override_counts" not in second  # nothing spent

    def test_the_rule_returns_none_and_logs_when_capped(self, monkeypatch):
        events = []

        class Recorder:
            def __getattr__(self, level):
                return lambda event, **kw: events.append((level, event, kw))

        monkeypatch.setattr(dp, "logger", Recorder())
        dp.register_decision_point_backend("fake", lambda i: hint_to("judge_verify"))
        p = redirecting(max_overrides=0)
        state = {**PROPOSES_COMPILE, **dp.wrap_node("extract", node_returning({}), [p])(dict(PROPOSES_COMPILE))}
        assert state["decision_outcomes"]["extract.post"]["capped"] is True
        assert dp.rule_for(p).decide(state, "compile_report") is None
        capped = [e for e in events if e[1] == "decision_point_override_capped"]
        assert capped and capped[0][0] == "warning" and capped[0][2]["slot"] == "extract.post"
        assert capped[0][2]["max_overrides"] == 0

    def test_a_hint_equal_to_the_proposal_spends_nothing(self):
        dp.register_decision_point_backend("fake", lambda i: hint_to("compile_report"))
        p = policy(allow=("compile_report",))
        out = dp.wrap_node("extract", node_returning({}), [p])(dict(PROPOSES_COMPILE))
        assert "decision_override_counts" not in out
        assert out["decision_outcomes"]["extract.post"]["vetted_route"] == "compile_report"

    def test_parks_are_exempt_from_the_budget(self):
        dp.register_decision_point_backend("fake", lambda i: PARK)
        p = redirecting(max_overrides=0)
        out = dp.wrap_node("extract", node_returning({}), [p])(dict(PROPOSES_COMPILE))
        stored = out["decision_outcomes"]["extract.post"]
        assert stored["vetted_route"] == "human_review" and "capped" not in stored
        assert "decision_override_counts" not in out

    def test_fail_safe_keeps_parking_with_a_spent_budget(self):
        p = redirecting(max_overrides=0, on_failure="fail_safe")
        out = dp.wrap_node("extract", node_returning({}), [p])(dict(PROPOSES_COMPILE))  # no backend
        assert out["decision_outcomes"]["extract.post"]["vetted_route"] == "human_review"

    def two_slots(self, first, second, *, first_hint="judge_verify", second_hint="boss_escalation"):
        extra = {"causes": ["fee_mismatch"]} if first_hint == "human_review" else {}  # a park needs causes
        dp.register_decision_point_backend("first", lambda i: hint_to(first_hint, **extra))
        dp.register_decision_point_backend("second", lambda i: hint_to(second_hint))
        a = policy(slot="extract.a", backend="first", allow=("judge_verify", "human_review"), **first)
        b = policy(slot="extract.b", backend="second", allow=("boss_escalation", "judge_verify"), **second)
        return dp.wrap_node("extract", node_returning({}), [a, b])(dict(PROPOSES_COMPILE))

    def test_only_the_slot_whose_rule_answers_is_charged(self):
        out = self.two_slots({}, {})
        assert out["decision_override_counts"] == {"extract.a": 1}  # a wins; b's rule never runs
        assert out["decision_outcomes"]["extract.b"]["vetted_route"] == "boss_escalation"
        assert "capped" not in out["decision_outcomes"]["extract.b"]

    def test_a_park_shadowed_by_an_earlier_slot_is_not_a_review_cause(self):
        dp.register_decision_point_backend("first", lambda i: hint_to("judge_verify"))
        dp.register_decision_point_backend("second", lambda i: dict(PARK))
        a = policy(slot="extract.a", backend="first", allow=("judge_verify",))
        b = policy(slot="extract.b", backend="second", allow=("human_review",))
        out = dp.wrap_node("extract", node_returning({}), [a, b])(dict(PROPOSES_COMPILE))
        assert out["decision_outcomes"]["extract.b"]["shadowed"] is True
        assert "shadowed" not in out["decision_outcomes"]["extract.a"]
        assert collect_review_causes(out) == []  # b never parked the document

    def test_a_capped_first_slot_does_not_shield_the_second(self):
        out = self.two_slots({"max_overrides": 0}, {})
        assert out["decision_override_counts"] == {"extract.b": 1}
        assert out["decision_outcomes"]["extract.a"]["capped"] is True

    def test_a_first_slot_that_agrees_with_the_router_does_not_shield_the_second(self):
        out = self.two_slots({}, {}, first_hint="compile_report")  # compile_report not in a's allow
        assert out["decision_outcomes"]["extract.a"]["vetted_route"] is None
        assert out["decision_override_counts"] == {"extract.b": 1}

    def test_a_first_slot_that_parks_shields_the_second_without_being_charged(self):
        out = self.two_slots({}, {}, first_hint="human_review")
        assert "decision_override_counts" not in out
        assert out["decision_outcomes"]["extract.a"]["vetted_route"] == "human_review"
        assert "capped" not in out["decision_outcomes"]["extract.b"]

    def test_each_slot_keeps_its_own_count_across_passes(self):
        out = self.two_slots({"max_overrides": 0}, {"max_overrides": 2})
        dp.register_decision_point_backend("second", lambda i: hint_to("boss_escalation"))
        wrapped = dp.wrap_node("extract", node_returning({}), [
            policy(slot="extract.a", backend="first", allow=("judge_verify",), max_overrides=0),
            policy(slot="extract.b", backend="second", allow=("boss_escalation",), max_overrides=2),
        ])
        state = {**PROPOSES_COMPILE, **out}
        state = {**state, **wrapped(state)}
        assert state["decision_override_counts"] == {"extract.b": 2}
        again = wrapped(state)
        assert again["decision_outcomes"]["extract.b"]["capped"] is True

    def test_a_router_that_raises_counts_as_an_override(self, monkeypatch):
        dp.register_decision_point_backend("fake", lambda i: hint_to("judge_verify"))
        monkeypatch.setitem(dp._SPEC_BY_NODE, "extract", ROUTE_SPECS[0].__class__(
            "extract", lambda s: 1 / 0, {"judge_verify": "judge_verify", "human_review": "human_review"}))
        out = dp.wrap_node("extract", node_returning({}), [redirecting()])(dict(PROPOSES_COMPILE))
        assert out["decision_override_counts"] == {"extract.post": 1}

    def test_garbage_counts_in_state_are_treated_as_zero(self):
        dp.register_decision_point_backend("fake", lambda i: hint_to("judge_verify"))
        out = dp.wrap_node("extract", node_returning({}), [redirecting()])(
            {**PROPOSES_COMPILE, "decision_override_counts": {"extract.post": "lots"}})
        assert out["decision_override_counts"] == {"extract.post": 1}


# Cycle across slots: extract -> judge_verify -> arbiter -> retry_extract -> judge_verify ...
CYCLE = {
    "extract.post": {"node": "extract", "backend": "to_judge", "allow": ["judge_verify"]},
    "judge_verify.post": {"node": "judge_verify", "backend": "to_arbiter", "allow": ["arbiter"]},
    "arbiter.post": {"node": "arbiter", "backend": "to_retry", "allow": ["retry_extract"]},
    "retry_extract.post": {"node": "retry_extract", "backend": "to_judge", "allow": ["judge_verify"]},
}


def register_cycle_backends():
    for name, dest in (("to_judge", "judge_verify"), ("to_arbiter", "arbiter"), ("to_retry", "retry_extract")):
        dp.register_decision_point_backend(name, lambda i, d=dest: hint_to(d))


def arbiter_accepts(mock_openai_client):
    """The scripted arbiter (legacy OpenAI-style client) always accepts with caveats, so
    its router proposes compile_report instead of parking and the cycle can run."""
    mock_openai_client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
        "decision": "accept_with_caveats", "fields_to_fix": [], "reasoning": "sound",
        "handoff_summary": "ok",
    })


def stream_doc(temp_base_dir, mock_langchain_llm, name="doc.txt", thread="dp-stream"):
    """Run one document through a compiled graph; returns (visited nodes in order, final state).

    A park is an ``interrupt()`` inside ``human_review``, which streams no node update, so
    ``human_review`` is appended to the visited list when the interrupt event arrives.
    """
    mock_langchain_llm.classification = {
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "contract",
    }
    for sub in ("inbox", "processing", "classified", "review", "failed"):
        (temp_base_dir / "pipeline" / sub).mkdir(parents=True, exist_ok=True)
    path = temp_base_dir / "pipeline" / "inbox" / name
    path.write_text("MASTER SERVICES AGREEMENT between Acme Corp. and Beta LLC.")
    state = {
        "doc_id": "", "matter_id": "TEST-MATTER", "original_filename": name, "stage": "inbox",
        "doc_type": None, "classification_confidence": None, "classification_attempts": 0,
        "extracted_data": None, "extraction_confidence": None, "extraction_attempts": 0,
        "trace_id": None, "escalation_reason": None, "review_decision": None, "retry_count": 0,
        "conflict_detected": False, "file_path": str(path), "doc_text": "", "error_message": None,
        "messages": [],
    }
    graph = bg.build_graph()
    visited: list[str] = []
    for event in graph.stream(state, {"configurable": {"thread_id": thread}}, stream_mode="updates"):
        visited.extend("human_review" if k == "__interrupt__" else k for k in event)
    return visited, graph.get_state({"configurable": {"thread_id": thread}}).values


class TestCompiledRedirects:
    """A slot hints an allowed destination other than human_review and the document takes it."""

    def test_default_path_for_comparison(self, temp_base_dir, mock_openai_client, mock_langchain_llm):
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert visited == ["intake", "classify", "extract", "compile_report", "catalog_write", "archive"]
        assert final["stage"] == "archived"

    def test_classify_slot_redirects_to_retry_classify(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"classify.post": {"node": "classify", "backend": "fake", "allow": ["retry_classify"]}})
        dp.register_decision_point_backend("fake", lambda i: hint_to("retry_classify", label="recheck"))
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert visited[:4] == ["intake", "classify", "retry_classify", "extract"]
        assert final["stage"] == "archived"
        assert final["decision_outcomes"]["classify.post"]["label"] == "recheck"
        assert final["decision_override_counts"] == {"classify.post": 1}

    def test_extract_slot_redirects_to_judge_verify(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"extract.post": {"node": "extract", "backend": "fake", "allow": ["judge_verify"]}})
        dp.register_decision_point_backend("fake", lambda i: hint_to("judge_verify"))
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert visited[:5] == ["intake", "classify", "extract", "judge_verify", "compile_report"]
        assert final["decision_override_counts"] == {"extract.post": 1}

    def test_a_hint_outside_allow_changes_nothing(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"classify.post": {"node": "classify", "backend": "fake", "allow": ["extract"]}})
        dp.register_decision_point_backend("fake", lambda i: hint_to("retry_classify"))
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert "retry_classify" not in visited and final["stage"] == "archived"
        assert "decision_override_counts" not in final

    def test_cyclic_allow_config_terminates_and_the_document_finishes(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        arbiter_accepts(mock_openai_client)
        decision_points(CYCLE)
        register_cycle_backends()
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)  # no GraphRecursionError
        assert {"judge_verify", "arbiter", "retry_extract"} <= set(visited)  # the cycle really ran
        assert final["decision_override_counts"] == {
            "extract.post": 1, "judge_verify.post": 1, "arbiter.post": 1, "retry_extract.post": 1}
        assert final["stage"] == "archived"
        assert any(o.get("capped") for o in final["decision_outcomes"].values())

    def test_a_larger_budget_still_terminates_within_the_limit(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        arbiter_accepts(mock_openai_client)
        decision_points({k: {**v, "max_overrides": 2} for k, v in CYCLE.items()})  # 8, the total cap
        register_cycle_backends()
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        counts = final["decision_override_counts"]
        assert max(counts.values()) == 2 and sum(counts.values()) <= 8
        assert final["stage"] == "archived"

    def test_without_a_budget_the_same_cycle_never_finishes(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm, monkeypatch
    ):
        """Control: the budget is what ends the cycle (the real cap is lifted for this test).

        Unbounded, the loop only stops when the run's output-token budget aborts it (or
        LangGraph's recursion limit, 10007 steps by default, whichever comes first).
        """
        from langgraph.errors import GraphRecursionError

        arbiter_accepts(mock_openai_client)
        monkeypatch.setattr(dp, "MAX_TOTAL_OVERRIDES", 1_000_000)
        monkeypatch.setattr(limits, "get_max_total_output_tokens", lambda: 2000)  # end the loop sooner
        decision_points({k: {**v, "max_overrides": 10_000} for k, v in CYCLE.items()})
        register_cycle_backends()
        with pytest.raises((GraphRecursionError, limits.RunBudgetExceeded)):
            stream_doc(temp_base_dir, mock_langchain_llm)

    def test_zero_budget_never_redirects(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"classify.post": {"node": "classify", "backend": "fake", "allow": ["retry_classify"],
                                           "max_overrides": 0}})
        dp.register_decision_point_backend("fake", lambda i: hint_to("retry_classify"))
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert "retry_classify" not in visited and final["stage"] == "archived"
        assert final["decision_outcomes"]["classify.post"]["capped"] is True

    def test_parks_still_work_with_a_zero_budget(
        self, decision_points, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points({"extract.post": {**SLOT["extract.post"], "max_overrides": 0}})
        dp.register_decision_point_backend("fake", lambda i: {**PARK})
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert visited[-1] == "human_review"


# ---------------------------------------------------------------------------
# Backend resolution at build time
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_module(tmp_path, monkeypatch):
    """A throwaway importable module ``dp_backend_mod`` with a working and a broken attribute."""
    import sys

    (tmp_path / "dp_backend_mod.py").write_text(
        "def decide(inp):\n"
        "    return {'route_hint': 'human_review', 'causes': ['from_import'], 'confidence': 0.9}\n"
        "class Holder:\n"
        "    @staticmethod\n"
        "    def nested(inp):\n"
        "        return {'label': 'nested'}\n"
        "NOT_CALLABLE = 3\n"
    )
    (tmp_path / "dp_broken_mod.py").write_text("raise RuntimeError('boom at import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield
    for name in ("dp_backend_mod", "dp_broken_mod"):
        sys.modules.pop(name, None)


class TestBackendResolution:
    def slot(self, backend, **extra):
        return {"extract.post": {"node": "extract", "backend": backend, "allow": ["human_review"], **extra}}

    def test_import_spec_is_resolved_registered_and_consulted(self, decision_points, backend_module):
        decision_points(self.slot("dp_backend_mod:decide"))
        (p,) = dp.prepare_slots()
        assert dp.get_decision_point_backend("dp_backend_mod:decide") is not None
        got = dp.consult(p, inp())
        assert got.status == "ok" and got.causes == ("from_import",)

    def test_dotted_attribute_path(self, decision_points, backend_module):
        decision_points(self.slot("dp_backend_mod:Holder.nested"))
        (p,) = dp.prepare_slots()
        assert dp.consult(p, inp()).label == "nested"

    @pytest.mark.parametrize(
        "spec, needle",
        [
            ("no_such_module_dp:fn", "ModuleNotFoundError"),
            ("dp_backend_mod:missing", "AttributeError"),
            ("dp_broken_mod:fn", "RuntimeError: boom at import"),
            ("dp_backend_mod:NOT_CALLABLE", "is not callable"),
            ("dp_backend_mod:a:b", "not a registered name or an import spec"),
            ("bad module:fn", "not a registered name or an import spec"),
            (":fn", "not a registered name or an import spec"),
        ],
    )
    def test_bad_import_specs_are_config_errors(self, decision_points, backend_module, spec, needle):
        decision_points(self.slot(spec))
        with pytest.raises(DecisionPointConfigError) as err:
            dp.prepare_slots()
        assert needle in str(err.value) and "decision_points.extract.post" in str(err.value)

    def test_build_graph_fails_on_a_bad_import_spec(self, decision_points, backend_module):
        decision_points(self.slot("dp_backend_mod:missing"))
        with pytest.raises(DecisionPointConfigError, match="cannot import backend"):
            bg.build_graph()

    def test_a_disabled_slot_is_not_imported(self, decision_points, backend_module):
        decision_points(self.slot("no_such_module_dp:fn", enabled=False))
        assert dp.prepare_slots() == ()

    def test_unregistered_bare_name_warns_once_per_slot_naming_on_failure(self, decision_points, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        decision_points({
            **self.slot("not_yet", on_failure="fail_safe"),
            "classify.post": {"node": "classify", "backend": "also_not_yet"},
            "judge_verify.post": {"node": "judge_verify", "backend": "not_yet", "enabled": False},
        })
        dp.prepare_slots()
        warned = [e for e in rec.events if e[1] == "decision_point_backend_unregistered"]
        assert [(w[0], w[2]["slot"], w[2]["on_failure"]) for w in warned] == [
            ("warning", "extract.post", "fail_safe"), ("warning", "classify.post", "fail_open")]

    def test_unregistered_warning_is_once_per_slot_per_process(self, decision_points, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        decision_points(self.slot("not_yet"))
        for _ in range(3):  # startup, then every build_graph()
            dp.prepare_slots()
        assert len([e for e in rec.events if e[1] == "decision_point_backend_unregistered"]) == 1

    def test_async_import_specs_are_config_errors(self, decision_points, backend_module, tmp_path):
        (tmp_path / "dp_backend_mod.py").write_text(
            (tmp_path / "dp_backend_mod.py").read_text() + "async def coro(inp):\n    return {}\n")
        decision_points(self.slot("dp_backend_mod:coro"))
        with pytest.raises(DecisionPointConfigError, match="is async"):
            dp.prepare_slots()

    def test_registered_bare_name_does_not_warn(self, decision_points, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(dp, "logger", rec)
        dp.register_decision_point_backend("known", lambda i: ok())
        decision_points(self.slot("known"))
        dp.prepare_slots()
        assert not [e for e in rec.events if e[1] == "decision_point_backend_unregistered"]

    def test_lazy_registration_after_the_build_still_works(self, decision_points, monkeypatch):
        decision_points(self.slot("late"))
        (p,) = dp.prepare_slots()  # warns, does not raise
        assert dp.consult(p, inp()).evidence["reason"] == "backend_not_registered"
        dp.register_decision_point_backend("late", lambda i: ok(label="here"))
        assert dp.consult(p, inp()).label == "here"

    def test_import_spec_backend_through_a_compiled_graph(
        self, decision_points, backend_module, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        decision_points(self.slot("dp_backend_mod:decide"))
        visited, final = stream_doc(temp_base_dir, mock_langchain_llm)
        assert visited[-1] == "human_review"
        assert final["decision_outcomes"]["extract.post"]["backend"] == "dp_backend_mod:decide"


def test_state_declares_the_key():
    from graph.state import DocumentState

    assert "decision_outcomes" in DocumentState.__annotations__


def test_input_state_view_type():
    assert isinstance(inp().state, MappingProxyType)


# ---------------------------------------------------------------------------
# Round-3 hardening: hostile backends, bounds, never-raise
# ---------------------------------------------------------------------------


class Chameleon(str):
    """A str that claims to equal both a park and a redirect."""

    def __eq__(self, other):
        return other in ("human_review", "retry_extract")

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = str.__hash__


class TestHostileAnswers:
    def test_a_lazy_slow_mapping_is_unavailable_within_the_timeout(self):
        from collections.abc import Mapping

        release = threading.Event()

        class Slow(Mapping):
            def __getitem__(self, key):
                release.wait(10)
                raise KeyError(key)

            def __iter__(self):
                return iter(())

            def __len__(self):
                return 0

        dp.register_decision_point_backend("fake", lambda i: Slow())  # returns at once; reading it stalls
        try:
            started = time.monotonic()
            got = dp.consult(policy(timeout_seconds=0.3), inp())
            assert time.monotonic() - started < 2.5
            assert got.status == "unavailable" and got.evidence["reason"] == "timeout"
        finally:
            release.set()

    def test_endless_causes_are_invalid_and_quick(self):
        import itertools

        for causes in (itertools.repeat("a"), (f"c{i}" for i in itertools.count())):
            dp.register_decision_point_backend("fake", lambda i, c=causes: {"causes": c, "route_hint": "human_review"})
            started = time.monotonic()
            got = dp.consult(policy(timeout_seconds=5), inp())
            assert time.monotonic() - started < 2
            assert got.status == "invalid" and got.evidence["reason"] == "malformed_answer"

    def test_more_than_eight_causes_are_invalid_even_with_duplicates(self):
        with pytest.raises(ValueError):
            ok(causes=["a"] * 9)
        assert ok(causes=["a"] * 8).causes == ("a",)

    def test_a_str_subclass_is_stored_as_an_exact_str(self):
        out = ok(route_hint=Chameleon("retry_extract"), label=Chameleon("l"), model=Chameleon("m"),
                 causes=[Chameleon("c1")], status=Chameleon("ok"))
        for value in (out.route_hint, out.label, out.model, out.causes[0], out.status):
            assert type(value) is str
        record = out.to_dict()
        assert type(record["route_hint"]) is str and record["route_hint"] != "human_review"

    def test_a_float_subclass_confidence_is_an_exact_float(self):
        class Evil(float):
            def __le__(self, other):
                return True

            __ge__ = __le__

            def __float__(self):
                return 0.5

        with pytest.raises(ValueError):  # 7.0 is out of range whatever the subclass's comparisons say
            ok(confidence=Evil(7.0))

    def test_an_outcome_subclass_is_revalidated(self):
        class Sneaky(dp.DecisionOutcome):
            def __post_init__(self):  # skip the base validation, as a hostile subclass would
                pass

            @property
            def route_hint(self):
                return Chameleon("retry_extract")

            @route_hint.setter
            def route_hint(self, value):  # the dataclass __init__ assigns it
                pass

        got = dp.normalize_outcome(Sneaky())
        assert type(got) is dp.DecisionOutcome and type(got.route_hint) is str

        class Broken(dp.DecisionOutcome):
            def __post_init__(self):
                pass

            @property
            def causes(self):
                return ["x" * 100]

            @causes.setter
            def causes(self, value):
                pass

        bad = dp.normalize_outcome(Broken())
        assert bad.status == "invalid" and bad.evidence["reason"] == "malformed_answer"

    def test_a_chameleon_hint_cannot_dodge_the_override_budget(self):
        dp.register_decision_point_backend("fake", lambda i: {"route_hint": Chameleon("retry_extract"),
                                                              "confidence": 0.9})
        wrapped = dp.wrap_node(
            "extract", node_returning({}),
            [policy(allow=("retry_extract", "human_review"), max_overrides=1)],
        )
        state = dict(PROPOSES_COMPILE)
        state.update(wrapped(state))
        assert state["decision_override_counts"] == {"extract.post": 1}
        assert state["decision_outcomes"]["extract.post"]["vetted_route"] == "retry_extract"
        again = wrapped(state)
        record = again["decision_outcomes"]["extract.post"]
        assert record["capped"] is True and record["vetted_route"] is None

    @pytest.mark.parametrize("bad", ["x\n", "x\n\n", "\nx", "x y"])
    def test_a_token_with_a_trailing_newline_is_invalid(self, bad):
        with pytest.raises(ValueError):
            ok(causes=[bad])
        assert dp.normalize_outcome({"causes": [bad]}).status == "invalid"

    def test_a_system_exit_from_a_backend_is_invalid_not_raised(self):
        def leave(i):
            raise SystemExit(3)

        dp.register_decision_point_backend("fake", leave)
        got = dp.consult(policy(), inp())
        assert got.status == "invalid" and got.evidence == {"reason": "backend_error", "error": "SystemExit"}
        safe = dp.consult(policy(on_failure="fail_safe"), inp())
        assert safe.route_hint == "human_review" and safe.causes == ("decision_point_invalid",)

    def test_a_keyboard_interrupt_still_propagates(self):
        def stop(i):
            raise KeyboardInterrupt

        dp.register_decision_point_backend("fake", stop)
        with pytest.raises(KeyboardInterrupt):
            dp.consult(policy(), inp())

    def test_the_span_output_carries_no_label(self, monkeypatch):
        updates = []

        class Span:
            def update(self, **kw):
                updates.append(kw)

        import contextlib

        @contextlib.contextmanager
        def fake_observation(*a, **kw):
            yield Span()

        monkeypatch.setattr(dp, "observation", fake_observation)
        dp.register_decision_point_backend("fake", lambda i: ok(label="Acme owes Beta"))
        dp.consult(policy(), inp())
        (call,) = updates
        assert "label" not in call["output"] and call["output"]["label_length"] == len("Acme owes Beta")
        assert "Acme" not in json.dumps(updates, default=str)


class TestBounds:
    @pytest.mark.parametrize("timeout", [10**400, float("inf"), 10**30])
    def test_a_huge_timeout_runs_the_call_and_returns_its_answer(self, timeout):
        dp.register_decision_point_backend("fake", lambda i: ok(label="fine"))
        got = dp.consult(policy(timeout_seconds=timeout), inp())
        assert got.status == "ok" and got.label == "fine"

    def test_a_nan_timeout_is_no_time_not_an_error(self):
        dp.register_decision_point_backend("fake", lambda i: ok())
        got = dp.consult(policy(timeout_seconds=float("nan")), inp())
        assert got.status == "unavailable" and got.evidence["reason"] == "timeout"

    @pytest.mark.parametrize("value", [3601, 10**400, float("inf"), float("nan"), -1])
    def test_config_rejects_an_unreasonable_timeout(self, decision_points, value):
        decision_points({"extract.post": {"node": "extract", "backend": "b", "timeout_seconds": value}})
        with pytest.raises(DecisionPointConfigError, match="timeout_seconds must be a number above 0 and at most 3600"):
            get_decision_points_config()

    def test_config_accepts_the_upper_bound(self, decision_points):
        decision_points({"extract.post": {"node": "extract", "backend": "b", "timeout_seconds": 3600}})
        assert get_decision_points_config()["extract.post"]["timeout_seconds"] == 3600.0

    def test_healthy_concurrent_calls_are_never_refused(self):
        cap, callers = 2, 8
        live: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(callers)
        results, errors = [], []

        def call():
            time.sleep(0.05)
            return 1

        def caller():
            barrier.wait()
            try:
                results.append(dp.run_bounded(call, name="t", abandoned=live, max_abandoned=cap, lock=lock, timeout=5))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=caller) for _ in range(callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert errors == [] and results == [1] * callers  # in-flight calls do not count
        assert live == []

    def test_stalled_calls_fill_the_cap_and_refuse_new_ones(self):
        live: list = []
        lock = threading.Lock()
        release = threading.Event()
        try:
            for _ in range(2):
                with pytest.raises(dp.DecisionBackendTimeout, match="exceeded"):
                    dp.run_bounded(lambda: release.wait(10), name="t", abandoned=live, max_abandoned=2, lock=lock, timeout=0.05)
            with pytest.raises(dp.DecisionBackendTimeout, match="2 stalled t calls"):
                dp.run_bounded(lambda: 1, name="t", abandoned=live, max_abandoned=2, lock=lock, timeout=1)
        finally:
            release.set()
        for t in list(live):
            t.join(5)
        assert dp.run_bounded(lambda: 1, name="t", abandoned=live, max_abandoned=2, lock=lock, timeout=1) == 1

    def test_finished_calls_free_their_place(self):
        live: list = []
        lock = threading.Lock()
        for _ in range(5):
            assert dp.run_bounded(lambda: 1, name="t", abandoned=live, max_abandoned=1, lock=lock, timeout=2) == 1


class TestResolveExit:
    def test_a_backend_module_that_exits_at_import_is_a_config_error(self, decision_points, tmp_path, monkeypatch):
        import sys

        (tmp_path / "dp_exit_mod.py").write_text("import sys\nsys.exit(3)\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        decision_points({"extract.post": {"node": "extract", "backend": "dp_exit_mod:fn"}})
        try:
            with pytest.raises(DecisionPointConfigError, match="SystemExit"):
                dp.prepare_slots()
        finally:
            sys.modules.pop("dp_exit_mod", None)

    def test_an_import_spec_with_a_trailing_newline_is_refused(self, backend_module):
        # Config strips whitespace, so this guards the resolver itself.
        with pytest.raises(DecisionPointConfigError, match="not a registered name or an import spec"):
            dp._resolve_backend(policy(backend="dp_backend_mod:decide\n"))


class TestDuplicateKeys:
    """The taxonomy loader refuses duplicated keys instead of keeping the last one."""

    SLOT = "  extract.post:\n    node: extract\n    backend: fake\n    on_failure: {mode}\n"

    def _load(self, tmp_path, monkeypatch, text):
        import pipeline.config as config

        path = tmp_path / "taxonomy.yaml"
        path.write_text(text)
        monkeypatch.setattr(config, "CONFIG_PATH", path)
        config.load_config.cache_clear()
        try:
            return config.load_config()
        finally:
            config.load_config.cache_clear()

    def test_a_slot_declared_twice_is_rejected(self, tmp_path, monkeypatch):
        import yaml

        text = "decision_points:\n" + self.SLOT.format(mode="fail_safe") + self.SLOT.format(mode="fail_open")
        with pytest.raises(yaml.YAMLError, match="duplicate key 'extract.post'"):
            self._load(tmp_path, monkeypatch, text)

    def test_a_setting_written_twice_in_one_slot_is_rejected(self, tmp_path, monkeypatch):
        import yaml

        text = "decision_points:\n" + self.SLOT.format(mode="fail_safe") + "    on_failure: fail_open\n"
        with pytest.raises(yaml.YAMLError, match="duplicate key 'on_failure'"):
            self._load(tmp_path, monkeypatch, text)

    def test_merge_keys_still_load(self, tmp_path, monkeypatch):
        got = self._load(tmp_path, monkeypatch, "base: &b {k: 1, j: 2}\nderived:\n  <<: *b\n  k: 3\n")
        assert got["derived"] == {"k": 3, "j": 2}

    def test_the_shipped_taxonomy_has_no_duplicate_keys(self):
        import yaml
        import pipeline.config as config

        with open(config.CONFIG_PATH) as f:
            assert "decision_points" in yaml.load(f, Loader=config._UniqueKeyLoader)
