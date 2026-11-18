"""Decision-model slots (graph/decision_slots.py): config, caps, routing, judge, resolver, focus.

Network-free: every decision model here is a fake callable; the LLM judge and the chat
reviewer are stubs that fail the test if a slot should have replaced them.
"""

from __future__ import annotations

import copy

import pytest

from agents.closed_field_resolver import register_closed_field_resolver
from agents.decision_backend import register_decision_backend
from agents.extraction_gate import register_extraction_gate
from graph import build_graph as bg
from graph import decision_slots as ds
from graph.route_spec import ROUTE_SPECS, clear_route_rules, decide_route, route_rules
from pipeline import limits
from pipeline.decision_review import DecisionAnswer
from pipeline.reconsideration import collect_review_causes

SPEC = {s.node: s for s in ROUTE_SPECS}
CONTRACT = {"parties": ["Acme Corp.", "Beta LLC"], "governing_law": "New York", "cuad_family": "service",
            "confidence": 0.99}


@pytest.fixture(autouse=True)
def _clean():
    """Rules, registries and the cap ledger are process-wide: start and end empty."""
    clear_route_rules()
    ds.reset_ledger()
    register_extraction_gate(None)
    register_closed_field_resolver(None)
    register_decision_backend(None)
    limits.reset_run_usage()
    yield
    clear_route_rules()
    ds.reset_ledger()
    register_extraction_gate(None)
    register_closed_field_resolver(None)
    register_decision_backend(None)
    limits.reset_run_usage()


@pytest.fixture
def slots(monkeypatch):
    """Set taxonomy ``decision_slots:`` (and optionally ``reviewer:``): ``slots({...})``."""
    import pipeline.config as config

    base = copy.deepcopy(config.load_config())

    def apply(section, reviewer=None):
        cfg = copy.deepcopy(base)
        cfg["decision_slots"] = section
        if reviewer is not None:
            cfg["reviewer"] = reviewer
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        return ds.get_slots()

    return apply


def gate(support, attempt=1, **extra):
    return {"doc_subclass": None, "attempt": attempt, "verdict": "accept", "field_support": support, **extra}


def extracted_state(**overrides):
    """A sound, confident contract extraction the base router archives."""
    state = {
        "doc_id": "d-slot", "doc_type": "contract", "doc_text": "MASTER SERVICES AGREEMENT ...",
        "classification_confidence": 0.99, "extracted_data": dict(CONTRACT), "extraction_confidence": 0.99,
        "extraction_attempts": 1, "extraction_guardrail": [], "conflict_detected": False,
    }
    state.update(overrides)
    return state


def route(node, state):
    return decide_route(SPEC[node], state)


def run_wrapped(node, update, state=None, cfg=None):
    """Run ``node``'s slot wrapper around a fake node returning ``update``; the merged state."""
    state = state if state is not None else {}
    fn = ds.wrap_node(node, lambda s: dict(update), cfg)
    ds.install_rules(cfg)
    return {**state, **fn(state)}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_taxonomy_is_all_baseline(self):
        cfg = ds.get_slots()
        assert cfg.nodes_to_wrap() == frozenset()
        assert (cfg.archive_gate, cfg.judge_replacement, cfg.arbiter_veto, cfg.resolver, cfg.field_review) == (
            "baseline",) * 5
        assert cfg.reviewer_park_cap is None
        assert (cfg.archive_park_cap, cfg.archive_release_cap) == (0.10, 0.03)

    def test_empty_and_none_are_baseline(self):
        assert ds.parse_slots(None) == ds.parse_slots({}) == ds.SlotsConfig()

    @pytest.mark.parametrize("raw, needle", [
        ({"archive_gate": {"mode": False}}, "YAML boolean"),
        ({"archive_gate": {"mode": "on"}}, "must be one of"),
        ({"archive_gate": {"park_cap": 1.5}}, "park_cap"),
        ({"archive_gate": {"release": "yes"}}, "release"),
        ({"archive_gate": {"bogus": 1}}, "unknown keys"),
        ({"nope": {}}, "unknown keys"),
        ({"cap_warmup": 0}, "cap_warmup"),
        ({"gate_support_min": float("nan")}, "gate_support_min"),
        ({"field_review": {"max_fields": 0}}, "max_fields"),
        ({"closed_field_resolver": {"mode": "auto"}}, "must be one of"),
        ([], "mapping"),
    ])
    def test_malformed_blocks_raise(self, raw, needle):
        with pytest.raises(ds.DecisionSlotConfigError, match=needle):
            ds.parse_slots(raw)

    def test_build_graph_fails_fast_on_a_bad_block(self, slots):
        with pytest.raises(ds.DecisionSlotConfigError):
            slots({"judge_replacement": {"mode": "always"}})

    def test_nodes_to_wrap(self):
        assert ds.parse_slots({"archive_gate": {"mode": "enforce"}}).nodes_to_wrap() == {
            "extract", "retry_extract", "judge_verify"}
        assert ds.parse_slots({"arbiter_veto": {"mode": "enforce"}}).nodes_to_wrap() == {"arbiter"}
        assert ds.parse_slots({"judge_replacement": {"mode": "gate"}}).nodes_to_wrap() == {"arbiter"}
        assert ds.parse_slots({"closed_field_resolver": {"mode": "resolve"}}).nodes_to_wrap() == frozenset()


# ---------------------------------------------------------------------------
# Gate answer and caps
# ---------------------------------------------------------------------------


class TestGateView:
    def test_verdict_is_derived_from_field_support_not_the_model_verdict(self):
        view = ds.gate_view({"extraction_attempts": 1,
                             "extraction_gate": gate({"parties": 0.9, "governing_law": 0.2}, verdict="accept")}, 0.5)
        assert view.available and not view.accepts
        assert view.unsupported == ("governing_law",) and view.min_support == 0.2

    @pytest.mark.parametrize("record, reason", [
        (None, "no_gate_answer"),
        ({"error": "timeout: x", "attempt": 1}, "gate_error"),
        ({"skipped": "parse_error", "attempt": 1}, "gate_skipped:parse_error"),
        (gate({"parties": 0.9}, attempt=1, ), None),
        (gate({"parties": 0.9}, attempt=None), "stale_answer"),
        (gate({}, attempt=2), "no_field_support"),
        (gate({"parties": True}, attempt=2), "malformed_field_support"),
        (gate({"parties": 1.2}, attempt=2), "malformed_field_support"),
    ])
    def test_unusable_answers(self, record, reason):
        view = ds.gate_view({"extraction_attempts": 2, "extraction_gate": record}, 0.5)
        assert not view.available
        assert view.reason == (reason or "stale_answer")


class TestCaps:
    def test_warmup_bounds_early_parks_and_releases(self):
        marks = []
        ds._mark_seen("s", marks)
        allowed = 0
        for i in range(5):
            m = [f"seen:s"]
            allowed += ds._charge("s", "park", 0.10, 20, m)
        assert allowed == 2  # 10% of max(1, 20)
        assert not ds._charge("s", "release", 0.03, 20, [])  # 3% of 20 < 1

    def test_release_opens_after_enough_documents(self):
        for _ in range(34):
            ds._mark_seen("s", [])
        assert ds._charge("s", "release", 0.03, 20, [])
        assert not ds._charge("s", "release", 0.03, 20, [])

    def test_a_document_is_counted_once(self):
        marks = []
        ds._mark_seen("s", marks)
        ds._mark_seen("s", marks)
        assert ds._charge("s", "park", 0.10, 20, marks) and ds._charge("s", "park", 0.10, 20, marks)
        assert ds.ledger_snapshot()["s"] == {"seen": 1, "park": 1, "release": 0}

    def test_no_cap(self):
        assert all(ds._charge("s", "park", None, 20, []) for _ in range(50))


# ---------------------------------------------------------------------------
# Archive gate
# ---------------------------------------------------------------------------


class TestArchiveGate:
    def test_baseline_wraps_nothing(self, slots):
        cfg = slots({})
        fn = lambda s: {}  # noqa: E731
        assert ds.wrap_node("extract", fn, cfg) is fn
        ds.install_rules(cfg)
        assert all(not r.name.startswith(ds.RULE_PREFIX) for s in ROUTE_SPECS for r in route_rules(s.node))

    def test_supported_extraction_archives(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        state = run_wrapped("extract", {"extraction_gate": gate({"parties": 0.95, "governing_law": 0.9})},
                            extracted_state(), cfg)
        assert route("extract", state) == "compile_report"
        assert state["decision_slot_outcomes"]["archive_gate"]["action"] == "agree"

    def test_unsupported_extraction_parks_with_a_cause(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        state = run_wrapped("extract", {"extraction_gate": gate({"parties": 0.95, "governing_law": 0.1})},
                            extracted_state(), cfg)
        rec = state["decision_slot_outcomes"]["archive_gate"]
        assert (rec["action"], rec["route"], rec["proposal"]) == ("park", "human_review", "compile_report")
        assert rec["unsupported_fields"] == ["governing_law"]
        assert route("extract", state) == "human_review"
        assert "archive_gate_unsupported" in collect_review_causes(state)

    def test_a_spent_park_cap_follows_the_baseline(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce", "park_cap": 0.0}})
        state = run_wrapped("extract", {"extraction_gate": gate({"governing_law": 0.1})}, extracted_state(), cfg)
        assert state["decision_slot_outcomes"]["archive_gate"]["action"] == "capped"
        assert route("extract", state) == "compile_report"
        assert "archive_gate_unsupported" not in collect_review_causes(state)

    def test_a_stale_or_missing_answer_never_routes(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        state = run_wrapped("retry_extract", {"extraction_gate": gate({"governing_law": 0.1}, attempt=1)},
                            extracted_state(extraction_attempts=2), cfg)
        assert state["decision_slot_outcomes"]["archive_gate"]["gate_reason"] == "stale_answer"
        assert route("retry_extract", state) == "compile_report"

    def test_a_rule_ignores_an_outcome_the_router_no_longer_proposes(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        state = run_wrapped("extract", {"extraction_gate": gate({"governing_law": 0.1})}, extracted_state(), cfg)
        state["extraction_confidence"] = 0.10  # the router now retries instead of archiving
        assert route("extract", state) == "retry_extract"

    def test_a_new_pass_of_the_node_replaces_its_decision(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        first = run_wrapped("extract", {"extraction_gate": gate({"governing_law": 0.1})}, extracted_state(), cfg)
        second = run_wrapped("extract", {"extraction_gate": gate({"governing_law": 0.9})}, first, cfg)
        assert second["decision_slot_outcomes"]["archive_gate"]["action"] == "agree"
        assert route("extract", second) == "compile_report"

    def test_transient_pass_clears_the_nodes_decision(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        first = run_wrapped("extract", {"extraction_gate": gate({"governing_law": 0.1})}, extracted_state(), cfg)
        second = run_wrapped("extract", {"transient_error": True}, first, cfg)
        assert "archive_gate" not in second["decision_slot_outcomes"]

    def _soft_park(self, **overrides):
        # The judge said complete, but the extraction stayed below its low band: a soft park.
        fields = dict(extraction_confidence=0.5, extraction_attempts=3, judge_verdict="complete",
                      judge_pass_count=1, retry_count=2)
        return extracted_state(**{**fields, **overrides})

    def test_release_is_off_unless_asked(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce"}})
        state = run_wrapped("judge_verify", {"extraction_gate": gate({"parties": 0.99}, attempt=3)},
                            self._soft_park(), cfg)
        assert route("judge_verify", state) == "human_review"
        assert "archive_gate" not in state.get("decision_slot_outcomes", {})

    def test_release_archives_a_soft_park(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce", "release": True, "release_cap": None}})
        base = self._soft_park()
        assert route("judge_verify", base) == "human_review"
        assert set(collect_review_causes(base)) <= ds._SOFT_CAUSES, collect_review_causes(base)
        state = run_wrapped("judge_verify", {"extraction_gate": gate({"parties": 0.99, "governing_law": 0.95},
                                                                      attempt=3)}, base, cfg)
        assert state["decision_slot_outcomes"]["archive_gate"]["action"] == "release"
        assert route("judge_verify", state) == "compile_report"

    @pytest.mark.parametrize("overrides, reason", [
        ({"judge_verdict": "incomplete", "judge_pass_count": 3}, "judge_incomplete"),
        ({"conflict_detected": True}, "conflict"),
        ({"extraction_guardrail": ["fabricated"]}, "guardrail"),
    ])
    def test_release_never_clears_hard_evidence(self, slots, overrides, reason):
        cfg = slots({"archive_gate": {"mode": "enforce", "release": True, "release_cap": None}})
        state = run_wrapped("judge_verify", {"extraction_gate": gate({"parties": 0.99}, attempt=3)},
                            self._soft_park(**overrides), cfg)
        rec = state["decision_slot_outcomes"]["archive_gate"]
        assert rec["action"] == "hold" and rec["hold_reason"].startswith(reason)
        assert route("judge_verify", state) == "human_review"

    def test_release_needs_every_field_at_the_release_bar(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce", "release": True, "release_cap": None}})
        state = run_wrapped("judge_verify", {"extraction_gate": gate({"parties": 0.99, "governing_law": 0.7},
                                                                      attempt=3)}, self._soft_park(), cfg)
        assert state["decision_slot_outcomes"]["archive_gate"]["hold_reason"] == "support_below_release_min"
        assert route("judge_verify", state) == "human_review"

    def test_release_cap_holds_in_warmup(self, slots):
        cfg = slots({"archive_gate": {"mode": "enforce", "release": True}})  # 3% cap, warmup 20
        state = run_wrapped("judge_verify", {"extraction_gate": gate({"parties": 0.99}, attempt=3)},
                            self._soft_park(), cfg)
        assert state["decision_slot_outcomes"]["archive_gate"]["action"] == "capped"
        assert route("judge_verify", state) == "human_review"


# ---------------------------------------------------------------------------
# Judge replacement and arbiter veto
# ---------------------------------------------------------------------------


@pytest.fixture
def no_llm_judge(monkeypatch):
    import agents.judge as judge

    def boom(*a, **k):
        raise AssertionError("the LLM judge ran")

    monkeypatch.setattr(judge, "CompletenessJudge", boom)


class TestJudgeReplacement:
    def _band(self, **overrides):
        # In the judge band: low <= confidence < judge_band_high.
        return extracted_state(extraction_confidence=0.92, **overrides)

    def test_gate_answer_is_the_verdict(self, slots, no_llm_judge):
        slots({"judge_replacement": {"mode": "gate"}})
        out = bg.judge_verify_node(self._band(extraction_gate=gate({"parties": 0.9, "governing_law": 0.95})))
        assert (out["judge_verdict"], out["judge_source"], out["judge_pass_count"]) == ("complete", "extraction_gate", 1)
        out = bg.judge_verify_node(self._band(extraction_gate=gate({"parties": 0.9, "governing_law": 0.3})))
        assert out["judge_verdict"] == "incomplete"
        assert out["judge_findings"] == ["governing_law: not supported by the source (p=0.30)"]
        assert out["decision_slot_outcomes"]["judge_replacement"]["action"] == "replaced"

    def test_without_a_usable_answer_the_llm_judge_runs(self, slots, monkeypatch):
        slots({"judge_replacement": {"mode": "gate"}})
        monkeypatch.setattr(bg, "_judge_verify", lambda s: {"judge_verdict": "partial", "transient_error": False})
        out = bg.judge_verify_node(self._band(extraction_gate=gate({"parties": 0.9}, attempt=0)))
        assert (out["judge_verdict"], out["judge_source"]) == ("partial", "llm")
        assert out["decision_slot_outcomes"]["judge_replacement"]["gate_reason"] == "stale_answer"

    def test_baseline_never_reads_the_gate(self, slots, monkeypatch):
        slots({})
        monkeypatch.setattr(bg, "_judge_verify", lambda s: {"judge_verdict": "complete", "transient_error": False})
        out = bg.judge_verify_node(self._band(extraction_gate=gate({"parties": 0.1})))
        assert out == {"judge_verdict": "complete", "transient_error": False}

    def test_accept_with_caveats_never_archives_a_gate_incomplete(self, slots):
        cfg = slots({"judge_replacement": {"mode": "gate"}})
        base = extracted_state(judge_verdict="incomplete", judge_source="extraction_gate", judge_pass_count=1)
        state = run_wrapped("arbiter", {"arbiter_decision": "accept_with_caveats"}, base, cfg)
        assert route("arbiter", state) == "human_review"
        assert "gate_incomplete" in collect_review_causes(state)
        # The rule is about the gate's verdict: an LLM judge's incomplete keeps today's route.
        state = run_wrapped("arbiter", {"arbiter_decision": "accept_with_caveats"},
                            {**base, "judge_source": "llm"}, cfg)
        assert route("arbiter", state) == "compile_report"


class TestArbiterVeto:
    def test_gate_disagreement_parks(self, slots):
        cfg = slots({"arbiter_veto": {"mode": "enforce"}})
        base = extracted_state(judge_verdict="partial", extraction_gate=gate({"parties": 0.2}))
        state = run_wrapped("arbiter", {"arbiter_decision": "accept_with_caveats"}, base, cfg)
        assert state["decision_slot_outcomes"]["arbiter_veto"]["action"] == "park"
        assert route("arbiter", state) == "human_review"
        assert "arbiter_veto_gate" in collect_review_causes(state)

    def test_agreement_and_other_decisions_keep_the_route(self, slots):
        cfg = slots({"arbiter_veto": {"mode": "enforce"}})
        base = extracted_state(judge_verdict="partial", extraction_gate=gate({"parties": 0.9}))
        state = run_wrapped("arbiter", {"arbiter_decision": "accept_with_caveats"}, base, cfg)
        assert route("arbiter", state) == "compile_report"
        weak = {**base, "extraction_gate": gate({"parties": 0.2})}
        state = run_wrapped("arbiter", {"arbiter_decision": "retry_extraction", "arbiter_retry_count": 1}, weak, cfg)
        assert route("arbiter", state) == "retry_extract"  # a veto only parks an archive

    def test_veto_never_releases(self, slots):
        cfg = slots({"arbiter_veto": {"mode": "enforce"}})
        base = extracted_state(judge_verdict="partial", extraction_gate=gate({"parties": 0.99}))
        state = run_wrapped("arbiter", {"arbiter_decision": "escalate"}, base, cfg)
        assert route("arbiter", state) == "human_review"


# ---------------------------------------------------------------------------
# Closed-field resolver
# ---------------------------------------------------------------------------


class Resolver:
    def __init__(self, value="service", probability=0.97):
        self.calls, self.value, self.probability = [], value, probability

    def __call__(self, doc_text, handoff):
        self.calls.append(handoff)
        return {"value": self.value, "probability": self.probability, "model": "fake-decisions"}


def _off_list(monkeypatch):
    monkeypatch.setattr(bg, "_extract_contracts",
                        lambda *a, **k: {**CONTRACT, "cuad_family": "services agreement thing", "confidence": 0.99})


def _classified():
    return {"doc_id": "d-cf", "doc_text": "MASTER SERVICES AGREEMENT between Acme Corp. and Beta LLC.",
            "doc_type": "contract", "classification_confidence": 0.99, "stage": "classified"}


class TestClosedFieldResolver:
    def test_baseline_parks_off_list_values(self, slots, monkeypatch):
        slots({})
        _off_list(monkeypatch)
        resolver = Resolver()
        register_closed_field_resolver(resolver)
        out = bg.extract_node(_classified())
        assert resolver.calls == [] and "closed_field_resolutions" not in out
        assert route("extract", {**_classified(), **out}) == "human_review"

    def test_resolve_applies_a_confident_on_list_pick(self, slots, monkeypatch):
        slots({"closed_field_resolver": {"mode": "resolve", "release_cap": None}})
        _off_list(monkeypatch)
        resolver = Resolver()
        register_closed_field_resolver(resolver)
        out = bg.extract_node(_classified())
        assert resolver.calls[0]["field"] == "cuad_family" and "service" in resolver.calls[0]["allowed"]
        assert out["extracted_data"]["cuad_family"] == "service"
        assert out["extracted_data"]["_closed_field_resolutions"][0]["original"] == "services agreement thing"
        assert out["closed_field_resolutions"][0]["applied"] is True
        assert route("extract", {**_classified(), **out}) != "human_review"

    @pytest.mark.parametrize("mode, answer", [
        ("suggest", Resolver()),
        ("resolve", Resolver(probability=0.5)),
        ("resolve", Resolver(value="banana")),
    ])
    def test_suggestions_and_unsure_picks_keep_the_park(self, slots, monkeypatch, mode, answer):
        slots({"closed_field_resolver": {"mode": mode, "release_cap": None}})
        _off_list(monkeypatch)
        register_closed_field_resolver(answer)
        out = bg.extract_node(_classified())
        assert out["extracted_data"]["cuad_family"] == "services agreement thing"
        assert out["closed_field_resolutions"][0]["applied"] is False
        assert route("extract", {**_classified(), **out}) == "human_review"

    def test_a_failing_resolver_keeps_the_park(self, slots, monkeypatch):
        slots({"closed_field_resolver": {"mode": "resolve", "release_cap": None}})
        _off_list(monkeypatch)

        def boom(doc_text, handoff):
            raise RuntimeError("decisions api down")

        register_closed_field_resolver(boom)
        out = bg.extract_node(_classified())
        assert out["closed_field_resolutions"][0]["status"] == "error"
        assert route("extract", {**_classified(), **out}) == "human_review"

    def test_release_cap_holds_in_warmup(self, slots, monkeypatch):
        slots({"closed_field_resolver": {"mode": "resolve"}})
        _off_list(monkeypatch)
        register_closed_field_resolver(Resolver())
        out = bg.extract_node(_classified())
        assert out["closed_field_resolutions"][0]["status"] == "capped"
        assert out["extracted_data"]["cuad_family"] == "services agreement thing"


# ---------------------------------------------------------------------------
# Sorter reviewer park cap
# ---------------------------------------------------------------------------

REVIEWER = {"default_mode": "llm", "by_class": {"contract": {"mode": "decision_only", "cutoff": 0.9}}}


class TestReviewerParkCap:
    def _state(self):
        return {"doc_id": "d-rv", "doc_text": "MASTER SERVICES AGREEMENT", "doc_type": "contract",
                "classification_confidence": 0.99}

    def test_no_cap_is_the_decision_lane(self, slots, monkeypatch):
        slots({}, reviewer=REVIEWER)
        register_decision_backend(lambda *a, **k: DecisionAnswer("correspondence", None, 0.99, "m"))
        monkeypatch.setattr(bg, "_llm_review_classify", lambda s: pytest.fail("chat reviewer ran"))
        out = bg.review_classify_node(self._state())
        assert out["review_route"] == "human_review"
        assert "decision_slot_outcomes" not in out

    def test_past_the_cap_the_chat_reviewer_decides(self, slots, monkeypatch):
        slots({"sorter_reviewer": {"park_cap": 0.0}}, reviewer=REVIEWER)
        register_decision_backend(lambda *a, **k: DecisionAnswer("correspondence", None, 0.99, "m"))
        monkeypatch.setattr(bg, "_llm_review_classify", lambda s: {"review_verdict": "reviewer_agrees_high",
                                                                  "reviewer_confidence": 0.99,
                                                                  "reviewer_doc_type": "contract"})
        out = bg.review_classify_node(self._state())
        assert out["review_route"] is None and out["reviewer_causes"] == []
        assert out["decision_slot_outcomes"]["sorter_reviewer"]["action"] == "capped"
        assert route("review_classify", {**self._state(), **out}) == "extract"

    def test_accepts_are_never_capped(self, slots, monkeypatch):
        slots({"sorter_reviewer": {"park_cap": 0.0}}, reviewer=REVIEWER)
        register_decision_backend(lambda *a, **k: DecisionAnswer("contract", "other", 0.99, "m"))
        monkeypatch.setattr(bg, "_llm_review_classify", lambda s: pytest.fail("chat reviewer ran"))
        out = bg.review_classify_node(self._state())
        assert out["review_route"] == "extract"
        assert out["decision_slot_outcomes"]["sorter_reviewer"]["action"] == "agree"


# ---------------------------------------------------------------------------
# Field-level review focus
# ---------------------------------------------------------------------------


class TestReviewFocus:
    def test_baseline_has_no_focus(self, slots):
        slots({})
        assert ds.review_focus(extracted_state(extraction_gate=gate({"parties": 0.1}))) is None

    def test_annotate_names_the_doubtful_fields(self, slots):
        slots({"field_review": {"mode": "annotate"}})
        state = extracted_state(
            extracted_data={**CONTRACT, "cuad_family": "banana"},
            extraction_gate=gate({"parties": 0.1, "governing_law": 0.9}),
            closed_field_resolutions=[{"field": "cuad_family", "suggested": "service", "probability": 0.6}],
        )
        assert ds.review_focus(state) == [
            {"field": "cuad_family", "reason": "out_of_vocabulary", "suggested": "service",
             "suggested_probability": 0.6},
            {"field": "parties", "reason": "unsupported", "support": 0.1},
        ]

    def test_too_many_fields_means_the_whole_document(self, slots):
        slots({"field_review": {"mode": "annotate", "max_fields": 1}})
        state = extracted_state(extraction_gate=gate({"parties": 0.1, "governing_law": 0.2}))
        assert ds.review_focus(state) is None


# ---------------------------------------------------------------------------
# Compiled graph (mock LLM)
# ---------------------------------------------------------------------------


def run_doc(temp_base_dir, mock_langchain_llm, name="slot.txt", thread="ds"):
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


def _slot_wrapped(graph):
    out = set()
    for name, node in graph.builder.nodes.items():
        fn = getattr(getattr(node, "runnable", None), "func", None)
        while fn is not None:
            if hasattr(fn, "mailroom_decision_slot_node"):
                out.add(name)
            fn = getattr(fn, "__wrapped__", None)
    return out


class TestCompiledGraph:
    def test_default_config_wraps_nothing(self, temp_base_dir, mock_openai_client, mock_langchain_llm):
        graph = bg.build_graph()
        assert _slot_wrapped(graph) == set()
        assert all(not r.name.startswith(ds.RULE_PREFIX) for s in ROUTE_SPECS for r in route_rules(s.node))

    def test_enforcing_gate_parks_an_unsupported_extraction(self, slots, temp_base_dir, mock_openai_client,
                                                            mock_langchain_llm):
        slots({"archive_gate": {"mode": "enforce", "park_cap": None}})
        register_extraction_gate(lambda text, handoff: {
            "verdict": "accept", "field_support": {name: 0.05 for name in handoff.get("fields", {})} or {"x": 0.05}})
        graph = bg.build_graph()
        assert _slot_wrapped(graph) == {"extract", "retry_extract", "judge_verify"}
        result = run_doc(temp_base_dir, mock_langchain_llm)
        interrupts = result.get("__interrupt__")
        assert interrupts, f"document was not parked: stage={result.get('stage')}"
        assert "archive_gate_unsupported" in interrupts[0].value["review_causes"]
        assert result["decision_slot_outcomes"]["archive_gate"]["action"] == "park"
        bg.reset_compiled_graph()
        assert all(not r.name.startswith(ds.RULE_PREFIX) for s in ROUTE_SPECS for r in route_rules(s.node))
