"""Routing as data (``graph/route_spec.py``): same topology, one declaration,
a safe hook for plug-in route rules.

The topology literals below were captured from ``build_graph()`` on the code
before the edges moved into ``route_spec`` (f83e398, v0.30.0), so they are an
independent record of what the compiled graph looked like, not a copy of the new
declaration.
"""
from __future__ import annotations

import threading
from typing import Literal, TypedDict

import pytest
import structlog
from langgraph.graph import END, START, StateGraph

from graph import routing
from graph import route_spec as rs
from graph.route_spec import (
    PLAIN_EDGES,
    ROUTE_SPECS,
    RouteRule,
    RouteSpec,
    RouteSpecError,
    add_route_rule,
    clear_route_rules,
    decide_route,
    remove_route_rule,
    route_rules,
    validate_route_specs,
    wire_edges,
)

EXPECTED_NODES = frozenset({
    "__end__",
    "__start__",
    "arbiter",
    "archive",
    "boss_escalation",
    "catalog_write",
    "classification_arbiter",
    "classify",
    "compile_report",
    "extract",
    "human_review",
    "intake",
    "judge_verify",
    "retry_classify",
    "retry_extract",
    "review_classify",
})

# (source, target, conditional) as snapshotted from build_graph() at f83e398 (v0.30.0).
EXPECTED_EDGES = frozenset({
    ("__start__", "compile_report", True),
    ("__start__", "extract", True),
    ("__start__", "intake", True),
    ("arbiter", "arbiter", True),
    ("arbiter", "compile_report", True),
    ("arbiter", "human_review", True),
    ("arbiter", "retry_extract", True),
    ("archive", "__end__", False),
    ("boss_escalation", "boss_escalation", True),
    ("boss_escalation", "compile_report", True),
    ("boss_escalation", "human_review", True),
    ("catalog_write", "archive", False),
    ("classification_arbiter", "extract", True),
    ("classification_arbiter", "human_review", True),
    ("classify", "classify", True),
    ("classify", "extract", True),
    ("classify", "human_review", True),
    ("classify", "retry_classify", True),
    ("classify", "review_classify", True),
    ("compile_report", "catalog_write", True),
    ("compile_report", "human_review", True),
    ("extract", "boss_escalation", True),
    ("extract", "compile_report", True),
    ("extract", "extract", True),
    ("extract", "human_review", True),
    ("extract", "judge_verify", True),
    ("extract", "retry_extract", True),
    ("human_review", "__end__", True),
    ("human_review", "compile_report", True),
    ("human_review", "extract", True),
    ("intake", "classify", False),
    ("judge_verify", "arbiter", True),
    ("judge_verify", "compile_report", True),
    ("judge_verify", "human_review", True),
    ("judge_verify", "judge_verify", True),
    ("retry_classify", "extract", True),
    ("retry_classify", "human_review", True),
    ("retry_classify", "retry_classify", True),
    ("retry_classify", "review_classify", True),
    ("retry_extract", "boss_escalation", True),
    ("retry_extract", "compile_report", True),
    ("retry_extract", "human_review", True),
    ("retry_extract", "judge_verify", True),
    ("retry_extract", "retry_extract", True),
    ("review_classify", "classification_arbiter", True),
    ("review_classify", "extract", True),
    ("review_classify", "human_review", True),
    ("review_classify", "review_classify", True),
})

# node -> (branch name LangGraph derives from the router, router value -> target)
EXPECTED_BRANCHES = {
    "__start__": ("entry_route", {"compile_report": "compile_report", "extract": "extract", "intake": "intake"}),
    "arbiter": ("after_arbiter", {"arbiter": "arbiter", "compile_report": "compile_report", "human_review": "human_review", "retry_extract": "retry_extract"}),
    "boss_escalation": ("after_boss", {"boss_escalation": "boss_escalation", "compile_report": "compile_report", "human_review": "human_review"}),
    "classification_arbiter": ("after_classification_arbiter", {"extract": "extract", "human_review": "human_review"}),
    "classify": ("after_classify", {"classify": "classify", "extract": "extract", "human_review": "human_review", "retry_classify": "retry_classify", "review_classify": "review_classify"}),
    "compile_report": ("after_report", {"catalog_write": "catalog_write", "human_review": "human_review"}),
    "extract": ("after_extraction_gated", {"boss_escalation": "boss_escalation", "compile_report": "compile_report", "extract": "extract", "human_review": "human_review", "judge_verify": "judge_verify", "retry_extract": "retry_extract"}),
    "human_review": ("after_human_review", {"compile_report": "compile_report", "extract": "extract", "failed": "__end__"}),
    "judge_verify": ("after_judge", {"arbiter": "arbiter", "compile_report": "compile_report", "human_review": "human_review", "judge_verify": "judge_verify"}),
    "retry_classify": ("after_retry_classify", {"extract": "extract", "human_review": "human_review", "retry_classify": "retry_classify", "review_classify": "review_classify"}),
    "retry_extract": ("after_retry_extraction_gated", {"boss_escalation": "boss_escalation", "compile_report": "compile_report", "human_review": "human_review", "judge_verify": "judge_verify", "retry_extract": "retry_extract"}),
    "review_classify": ("after_review_classify", {"classification_arbiter": "classification_arbiter", "extract": "extract", "human_review": "human_review", "review_classify": "review_classify"}),
}

EXPECTED_PLAIN_EDGES = [("archive", "__end__"), ("catalog_write", "archive"), ("intake", "classify")]


@pytest.fixture(autouse=True)
def _no_leaked_rules():
    """The rule registry is process-wide: every test starts and ends empty."""
    clear_route_rules()
    yield
    clear_route_rules()


@pytest.fixture(scope="module")
def compiled():
    from graph.build_graph import build_graph

    return build_graph()


def _spec(router, destinations, node="classify", rules=()):
    return RouteSpec(node, router, destinations, tuple(rules))


# ---- topology is unchanged ---------------------------------------------------------------


class TestTopologyUnchanged:
    def test_nodes_and_edges_match_the_pre_refactor_graph(self, compiled):
        drawn = compiled.get_graph()
        assert frozenset(drawn.nodes) == EXPECTED_NODES
        assert frozenset((e.source, e.target, bool(e.conditional)) for e in drawn.edges) == EXPECTED_EDGES

    def test_conditional_maps_and_branch_names_match(self, compiled):
        got = {}
        for node, branches in compiled.builder.branches.items():
            assert len(branches) == 1, node
            (name, branch), = branches.items()
            got[node] = (name, dict(sorted(branch.ends.items())))
        assert got == EXPECTED_BRANCHES

    def test_plain_edges_match(self, compiled):
        assert sorted(compiled.builder.edges) == EXPECTED_PLAIN_EDGES

    def test_spec_table_is_exactly_the_conditional_nodes(self):
        assert {s.node for s in ROUTE_SPECS} == set(EXPECTED_BRANCHES)
        assert len(ROUTE_SPECS) == len(EXPECTED_BRANCHES) == 12  # START + 11 nodes
        assert sorted(PLAIN_EDGES) == EXPECTED_PLAIN_EDGES

    def test_routers_keep_their_public_names(self):
        by_node = {s.node: s.router for s in ROUTE_SPECS}
        assert by_node["classify"] is routing.after_classify
        assert by_node["extract"] is routing.after_extraction_gated
        assert by_node["retry_extract"] is routing.after_retry_extraction_gated
        assert by_node[START] is routing.entry_route

    def test_entry_route_is_still_importable_from_build_graph(self):
        from graph import build_graph as bg

        assert bg.entry_route is routing.entry_route

    def test_every_router_has_a_literal_annotation_so_the_agreement_check_is_not_vacuous(self):
        for spec in ROUTE_SPECS:
            assert rs._literal_values(spec.router) == frozenset(spec.destinations), spec.node


# ---- validation --------------------------------------------------------------------------


def _ab(state) -> Literal["a", "b"]:
    return "a"


class TestValidation:
    NODES = {"classify", "extract", "human_review"}

    def test_declared_topology_is_valid_for_the_real_nodes(self):
        nodes = {n for n in EXPECTED_NODES if not n.startswith("__")}
        validate_route_specs(ROUTE_SPECS, PLAIN_EDGES, nodes)

    def test_literal_annotation_and_destinations_must_agree(self):
        spec = _spec(_ab, {"a": "extract"})  # "b" is annotated but not mapped
        with pytest.raises(RouteSpecError) as exc:
            validate_route_specs([spec], [], {"classify", "extract"})
        assert "_ab" in str(exc.value) and "['b']" in str(exc.value)

    def test_extra_destination_not_in_annotation_is_caught(self):
        spec = _spec(_ab, {"a": "extract", "b": "extract", "c": "extract"})
        with pytest.raises(RouteSpecError, match=r"mapped but not annotated: \['c'\]"):
            validate_route_specs([spec], [], {"classify", "extract"})

    def test_unannotated_router_is_not_checked(self):
        validate_route_specs(
            [_spec(lambda s: "x", {"x": "extract"}), RouteSpec(START, lambda s: "classify", {"classify": "classify"})],
            [("extract", END)],
            {"classify", "extract"},
        )

    def test_destination_must_be_a_node_or_end(self):
        spec = _spec(lambda s: "x", {"x": "nowhere"})
        with pytest.raises(RouteSpecError, match="'nowhere', which is not a node or END"):
            validate_route_specs([spec], [], {"classify"})

    def test_spec_for_unknown_node(self):
        with pytest.raises(RouteSpecError, match="unknown node"):
            validate_route_specs([_spec(lambda s: "x", {"x": "classify"}, node="ghost")], [], {"classify"})

    def test_every_node_needs_exactly_one_outgoing_definition(self):
        entry = RouteSpec(START, lambda s: "classify", {"classify": "classify"})
        classify = _spec(lambda s: "extract", {"extract": "extract"})
        with pytest.raises(RouteSpecError, match="'extract': no outgoing edge"):
            validate_route_specs([entry, classify], [], {"classify", "extract"})
        with pytest.raises(RouteSpecError, match="'classify': 2 outgoing"):
            validate_route_specs([entry, classify], [("classify", "extract"), ("extract", END)], {"classify", "extract"})

    def test_plain_edge_endpoints_are_checked(self):
        with pytest.raises(RouteSpecError, match="unknown target"):
            validate_route_specs([], [("classify", "nope")], {"classify"})

    def test_wire_edges_adds_nothing_when_invalid(self):
        workflow = StateGraph(dict)
        workflow.add_node("classify", lambda s: {})
        with pytest.raises(RouteSpecError):
            wire_edges(workflow, [_spec(_ab, {"a": "classify"})], [])
        assert not workflow.branches and not workflow.edges

    def test_error_lists_every_problem(self):
        specs = [_spec(_ab, {"a": "ghost"}), _spec(lambda s: "x", {"x": "ghost"}, node="extract")]
        with pytest.raises(RouteSpecError) as exc:
            validate_route_specs(specs, [], {"classify", "extract"})
        text = str(exc.value)
        assert text.count("'ghost'") >= 2 and "disagree" in text


# ---- rule evaluation ---------------------------------------------------------------------

DESTS = {"keep": "extract", "review": "human_review"}


def _counting_router():
    calls = []

    def router(state):
        calls.append(state)
        return "keep"

    return router, calls


class TestRuleEvaluation:
    def test_no_rules_is_exactly_one_base_router_call_with_the_original_state(self):
        router, calls = _counting_router()
        state = {"x": 1}
        assert decide_route(_spec(router, DESTS), state) == "keep"
        assert len(calls) == 1 and calls[0] is state

    def test_first_non_none_rule_wins(self):
        router, calls = _counting_router()
        seen = []

        def abstain(state, proposed):
            seen.append("abstain")

        def first(state, proposed):
            seen.append("first")
            return "review"

        def second(state, proposed):
            seen.append("second")
            return "keep"

        spec = _spec(router, DESTS, rules=[RouteRule("abstain", abstain), RouteRule("first", first), RouteRule("second", second)])
        assert decide_route(spec, {}) == "review"
        assert seen == ["abstain", "first"]
        assert len(calls) == 1  # the base router proposed once; the winning rule overrode it

    def test_rule_receives_the_routers_proposal(self):
        router, calls = _counting_router()
        seen = []

        def spy(state, proposed):
            seen.append(proposed)

        spec = _spec(router, DESTS, rules=[RouteRule("spy", spy)])
        assert decide_route(spec, {}) == "keep"
        assert seen == ["keep"]

    def test_every_rule_sees_the_same_proposal_even_after_an_earlier_override_is_ignored(self):
        router, _ = _counting_router()
        seen = []

        def spy(state, proposed):
            seen.append(proposed)

        spec = _spec(router, DESTS, rules=[RouteRule("bad", lambda s, p: "elsewhere"), RouteRule("spy", spy)])
        assert decide_route(spec, {}) == "keep"
        assert seen == ["keep"]

    def test_router_is_called_exactly_once_even_when_a_rule_overrides(self):
        router, calls = _counting_router()
        state = {"x": 1}
        spec = _spec(router, DESTS, rules=[RouteRule("a", lambda s, p: None), RouteRule("b", lambda s, p: "review")])
        add_route_rule("classify", RouteRule("c", lambda s, p: "keep"))
        assert decide_route(spec, state) == "review"
        assert len(calls) == 1 and calls[0] is state

    def test_rule_returning_the_proposal_is_a_no_op_override(self):
        router, calls = _counting_router()
        spec = _spec(router, DESTS, rules=[RouteRule("echo", lambda s, p: p), RouteRule("later", lambda s, p: "review")])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "keep"  # same as the proposal, and it ends the evaluation
        assert len(calls) == 1
        applied = [l for l in logs if l["event"] == "route_rule_applied"]
        assert len(applied) == 1 and applied[0]["rule"] == "echo" and applied[0]["overrode"] is False

    def test_router_exception_is_not_swallowed(self):
        def broken(state):
            raise RuntimeError("router bug")

        spec = _spec(broken, DESTS, rules=[RouteRule("r", lambda s, p: "review")])
        with pytest.raises(RuntimeError, match="router bug"):
            decide_route(spec, {})

    def test_all_none_falls_through_to_the_base_router(self):
        router, calls = _counting_router()
        spec = _spec(router, DESTS, rules=[RouteRule("n", lambda s, p: None)])
        assert decide_route(spec, {}) == "keep"
        assert len(calls) == 1

    def test_unknown_destination_is_ignored_and_logged(self):
        router, _ = _counting_router()
        spec = _spec(router, DESTS, rules=[RouteRule("bad", lambda s, p: "elsewhere"), RouteRule("good", lambda s, p: "review")])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "review"
        warned = [l for l in logs if l["event"] == "route_rule_unknown_destination"]
        assert len(warned) == 1
        assert warned[0]["rule"] == "bad" and warned[0]["node"] == "classify" and warned[0]["log_level"] == "warning"

    def test_unknown_destination_alone_falls_back_to_base(self):
        router, calls = _counting_router()
        spec = _spec(router, DESTS, rules=[RouteRule("bad", lambda s, p: "elsewhere")])
        assert decide_route(spec, {}) == "keep"
        assert len(calls) == 1

    @pytest.mark.parametrize("answer", [3, ["review"], {"review": 1}, b"review", ""])
    def test_non_string_or_unhashable_answers_are_ignored(self, answer):
        router, _ = _counting_router()
        spec = _spec(router, DESTS, rules=[RouteRule("odd", lambda s, p, a=answer: a)])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "keep"
        assert [l["event"] for l in logs] == ["route_rule_unknown_destination"]

    def test_raising_rule_is_ignored_and_logged(self):
        router, _ = _counting_router()

        def boom(state, proposed):
            raise RuntimeError("model unavailable")

        spec = _spec(router, DESTS, rules=[RouteRule("boom", boom), RouteRule("good", lambda s, p: "review")])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "review"
        failed = [l for l in logs if l["event"] == "route_rule_failed"]
        assert len(failed) == 1 and failed[0]["rule"] == "boom" and "model unavailable" in failed[0]["error"]

    def test_answer_whose_repr_raises_cannot_crash_routing(self):
        router, calls = _counting_router()

        class Hostile:
            def __repr__(self):
                raise RuntimeError("repr exploded")

        spec = _spec(router, DESTS, rules=[RouteRule("hostile", lambda s, p: Hostile())])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "keep"
        warned = [l for l in logs if l["event"] == "route_rule_unknown_destination"]
        assert len(warned) == 1 and warned[0]["rule"] == "hostile"
        assert "Hostile" in warned[0]["answer_type"]
        assert len(calls) == 1

    def test_exception_whose_repr_raises_cannot_crash_routing(self):
        router, _ = _counting_router()

        class HostileError(Exception):
            def __repr__(self):
                raise RuntimeError("repr exploded")

            __str__ = __repr__

        def boom(state, proposed):
            raise HostileError()

        spec = _spec(router, DESTS, rules=[RouteRule("boom", boom), RouteRule("good", lambda s, p: "review")])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "review"
        failed = [l for l in logs if l["event"] == "route_rule_failed"]
        assert len(failed) == 1 and failed[0]["rule"] == "boom"
        assert failed[0]["error_type"] == "HostileError"

    def test_str_subclass_whose_hash_raises_cannot_crash_routing(self):
        router, calls = _counting_router()

        class UnhashableStr(str):
            def __hash__(self):
                raise RuntimeError("hash exploded")

        spec = _spec(router, DESTS, rules=[RouteRule("unhashable", lambda s, p: UnhashableStr("review"))])
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, {}) == "keep"
        assert [l["event"] for l in logs] == ["route_rule_unknown_destination"]
        assert len(calls) == 1

    def test_rules_see_a_read_only_view_and_the_state_is_not_mutated(self):
        router, calls = _counting_router()
        attempts = []

        def meddle(state, proposed):
            attempts.append(type(state).__name__)
            assert state["doc_id"] == "d1"
            state["doc_type"] = "hacked"  # must raise TypeError

        def meddle_delete(state, proposed):
            del state["doc_id"]

        spec = _spec(router, DESTS, rules=[RouteRule("meddle", meddle), RouteRule("del", meddle_delete)])
        state = {"doc_id": "d1"}
        with structlog.testing.capture_logs() as logs:
            assert decide_route(spec, state) == "keep"
        assert attempts == ["mappingproxy"]
        assert state == {"doc_id": "d1"}
        assert [l["event"] for l in logs] == ["route_rule_failed", "route_rule_failed"]
        assert calls[0] is state  # the base router still gets the real, mutable state

    def test_spec_rules_run_before_registered_rules(self):
        router, _ = _counting_router()
        spec = _spec(router, DESTS, node="classify", rules=[RouteRule("static", lambda s, p: "keep")])
        add_route_rule("classify", RouteRule("dynamic", lambda s, p: "review"))
        assert decide_route(spec, {}) == "keep"

    def test_spec_is_immutable(self):
        spec = _spec(lambda s: "keep", {"keep": "extract"})
        with pytest.raises(TypeError):
            spec.destinations["x"] = "y"
        with pytest.raises(Exception):
            spec.node = "other"
        assert isinstance(spec.rules, tuple)


# ---- registry ----------------------------------------------------------------------------


class TestRegistry:
    def test_add_orders_and_positions(self):
        for name in ("a", "b"):
            add_route_rule("extract", RouteRule(name, lambda s, p: None))
        add_route_rule("extract", RouteRule("first", lambda s, p: None), position="first")
        add_route_rule("extract", RouteRule("mid", lambda s, p: None), position=1)
        assert [r.name for r in route_rules("extract")] == ["first", "mid", "a", "b"]

    def test_remove_and_clear(self):
        add_route_rule("extract", RouteRule("a", lambda s, p: None))
        add_route_rule("classify", RouteRule("b", lambda s, p: None))
        assert remove_route_rule("extract", "a") is True
        assert remove_route_rule("extract", "a") is False
        assert route_rules("extract") == ()
        clear_route_rules("classify")
        assert route_rules("classify") == ()
        add_route_rule("classify", RouteRule("b", lambda s, p: None))
        add_route_rule("extract", RouteRule("a", lambda s, p: None))
        clear_route_rules()
        assert route_rules("classify") == route_rules("extract") == ()

    def test_unknown_node_duplicate_name_and_bad_position_are_rejected(self):
        with pytest.raises(ValueError, match="no route is declared"):
            add_route_rule("ghost", RouteRule("a", lambda s, p: None))
        add_route_rule("extract", RouteRule("a", lambda s, p: None))
        with pytest.raises(ValueError, match="already registered"):
            add_route_rule("extract", RouteRule("a", lambda s, p: None))
        with pytest.raises(ValueError, match="position"):
            add_route_rule("extract", RouteRule("b", lambda s, p: None), position="middle")
        assert [r.name for r in route_rules("extract")] == ["a"]

    @pytest.mark.parametrize("node", ["intake", "catalog_write", "archive"])
    def test_plain_edge_nodes_take_no_rules(self, node):
        assert node in {src for src, _ in PLAIN_EDGES}
        with pytest.raises(ValueError, match="no route is declared"):
            add_route_rule(node, RouteRule("a", lambda s, p: None))
        assert route_rules(node) == ()

    def test_start_can_take_rules(self):
        add_route_rule(START, RouteRule("a", lambda s, p: None))
        assert [r.name for r in route_rules(START)] == ["a"]

    def test_concurrent_adds_lose_nothing(self):
        threads, per_thread = 8, 25
        barrier = threading.Barrier(threads)

        def work(t):
            barrier.wait()
            for i in range(per_thread):
                add_route_rule("extract", RouteRule(f"t{t}-{i}", lambda s, p: None))

        pool = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
        for th in pool:
            th.start()
        for th in pool:
            th.join()
        names = [r.name for r in route_rules("extract")]
        assert len(names) == len(set(names)) == threads * per_thread

    def test_concurrent_add_and_remove_stay_consistent(self):
        errors = []

        def churn(t):
            try:
                for i in range(50):
                    rule = RouteRule(f"c{t}-{i}", lambda s, p: None)
                    add_route_rule("classify", rule)
                    assert remove_route_rule("classify", rule.name) is True
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        pool = [threading.Thread(target=churn, args=(t,)) for t in range(6)]
        for th in pool:
            th.start()
        for th in pool:
            th.join()
        assert not errors
        assert route_rules("classify") == ()

    def test_fixture_isolates_tests_part_1(self):
        add_route_rule("arbiter", RouteRule("leak-check", lambda s, p: None))
        assert route_rules("arbiter")

    def test_fixture_isolates_tests_part_2(self):
        assert route_rules("arbiter") == ()


# ---- rules change a real route -----------------------------------------------------------


class _State(TypedDict, total=False):
    path: list


def _stub(name):
    def node(state):
        return {"path": [*state.get("path", []), name]}

    node.__name__ = f"stub_{name}"
    return node


def _stub_graph(router):
    """START -> classify -> (router) extract | human_review -> END, stub nodes, real wiring."""
    workflow = StateGraph(_State)
    for name in ("classify", "extract", "human_review"):
        workflow.add_node(name, _stub(name))
    specs = [
        RouteSpec(START, lambda s: "classify", {"classify": "classify"}),
        RouteSpec("classify", router, {"extract": "extract", "human_review": "human_review"}),
    ]
    wire_edges(workflow, specs, [("extract", END), ("human_review", END)])
    return workflow.compile()


class TestRulesChangeCompiledRoutes:
    def test_stub_graph_follows_the_base_router_without_rules(self):
        graph = _stub_graph(lambda s: "extract")
        assert graph.invoke({"path": []})["path"] == ["classify", "extract"]

    def test_registered_rule_redirects_a_compiled_run_and_removal_restores_it(self):
        graph = _stub_graph(lambda s: "extract")
        add_route_rule("classify", RouteRule("to-review", lambda s, p: "human_review"))
        assert graph.invoke({"path": []})["path"] == ["classify", "human_review"]
        assert remove_route_rule("classify", "to-review")
        assert graph.invoke({"path": []})["path"] == ["classify", "extract"]

    def test_rule_reads_state_and_abstains_otherwise(self):
        graph = _stub_graph(lambda s: "extract")
        add_route_rule("classify", RouteRule("only-flagged", lambda s, p: "human_review" if "flag" in s["path"] else None))
        assert graph.invoke({"path": []})["path"] == ["classify", "extract"]
        assert graph.invoke({"path": ["flag"]})["path"] == ["flag", "classify", "human_review"]

    def test_rule_with_an_answer_outside_the_destinations_cannot_derail_a_run(self):
        graph = _stub_graph(lambda s: "extract")
        add_route_rule("classify", RouteRule("rogue", lambda s, p: "archive"))
        add_route_rule("classify", RouteRule("crash", lambda s, p: 1 / 0), position="first")
        assert graph.invoke({"path": []})["path"] == ["classify", "extract"]

    def test_real_graph_edge_router_honours_registered_rules(self, compiled):
        branch = compiled.builder.branches["compile_report"]["after_report"]
        state = {"extracted_data": {"_report": {"summary": "ok"}}}
        assert routing.after_report(state) == "catalog_write"
        assert branch.path.invoke(state) == "catalog_write"
        add_route_rule("compile_report", RouteRule("hold", lambda s, p: "human_review"))
        assert branch.path.invoke(state) == "human_review"
        assert routing.after_report(state) == "catalog_write"  # the router itself is untouched
        clear_route_rules()
        assert branch.path.invoke(state) == "catalog_write"

    def test_real_graph_start_router_honours_registered_rules(self, compiled):
        branch = compiled.builder.branches[START]["entry_route"]
        assert branch.path.invoke({}) == "intake"
        add_route_rule(START, RouteRule("skip-intake", lambda s, p: "extract"))
        assert branch.path.invoke({}) == "extract"
