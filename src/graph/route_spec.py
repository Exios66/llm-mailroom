"""Graph routing as data.

The pipeline's edge topology used to live twice: in each router's ``Literal[...]``
return annotation (``graph/routing.py``) and in the edge-map dicts written out in
``build_graph()``. This module is the one place that declares it, and the one hook
through which a route can be influenced without editing a router.

* :data:`ROUTE_SPECS` and :data:`PLAIN_EDGES` declare every edge of the graph.
  ``build_graph()`` derives its ``add_conditional_edges`` / ``add_edge`` calls
  from them through :func:`wire_edges`.
* :func:`validate_route_specs` fails fast when a destination is not a node of the
  graph, when a node has no (or two) outgoing definitions, or when a router's
  ``Literal`` return annotation disagrees with its destination map.
* A :class:`RouteRule` is an optional refinement of a route
  (``decide(state_view, proposed) -> destination | None``). The base router always
  decides first; ``proposed`` is its answer, and a rule may keep it (return ``None``
  or ``proposed``) or replace it with another declared destination. With no rule
  registered a route is decided by exactly one call of the base router, as before.
  Rules are added and removed process-wide with :func:`add_route_rule` /
  :func:`remove_route_rule` / :func:`clear_route_rules`.

Reach: rules apply only to the 12 conditional routes in :data:`ROUTE_SPECS` (START
plus 11 nodes). ``intake``, ``catalog_write`` and ``archive`` are plain edges
(:data:`PLAIN_EDGES`) with nothing to decide, and :func:`add_route_rule` refuses
them.

Rule semantics (the contract a decision model plugs into):

* The base router is called first, exactly once, with the original state object.
  Its answer is the ``proposed`` argument of every rule.
* Rules run in order: the spec's own ``rules`` first, then the registered ones in
  registration order (``position=`` places a rule at the front or at an index).
* The first rule that returns a declared destination wins; ``None`` means "no
  opinion" and the next rule decides. When no rule answers, ``proposed`` is
  returned. A rule that returns ``proposed`` is a no-op that ends the evaluation.
* A rule refines the router's proposal, it does not own the route. The router's
  self-loops (transient retry of ``classify`` / ``extract`` and the like) and its
  parks (``human_review``) carry the pipeline's safety budgets; a rule that
  overrides one of them is responsible for not bypassing those budgets
  (``graph.decision_points`` is the per-slot policy that does: its rules never override
  a park or a self-loop and spend a per-document ``max_overrides`` budget).
* A rule never gets to break routing. An answer that is not a key of the spec's
  ``destinations`` is not followed (logged as ``route_rule_unknown_destination``),
  and a rule that raises is ignored (logged as ``route_rule_failed``); in both
  cases evaluation falls through. Validation and logging cannot raise either: an
  answer or exception with a hostile ``__repr__`` or ``__hash__`` is logged by its
  type name. The base router is never validated or wrapped: its answer (and its
  exceptions) pass through untouched.
* A rule receives a read-only view of the state (``types.MappingProxyType``), so it
  cannot reassign or delete top-level keys. Nested values are the live objects;
  a rule must treat them as read-only too.
* Rules are pure and synchronous: a rule reads state, it does not call a model or
  write state. Work that produces evidence (a decision model's answer, a review
  cause) belongs in a node, which writes it to state for a rule to read.
  ``add_route_rule`` refuses ``async def`` rules.
* A rule that redirects to ``human_review`` must only do so on state that already
  carries a review cause. ``human_review_node`` derives the park reason from state
  (``pipeline.reconsideration.collect_review_causes``), not from the route, so a
  rule that parks a clean document parks it with no cause or reason, and
  ``test_review_cause_coverage.py`` (which covers the routers) cannot see it.
* The routers are bound here at import time. Patching ``graph.build_graph.after_x``
  no longer changes the graph; patch ``graph.routing`` before import, or register a
  rule.
* ``route_rule_applied`` is logged at INFO when a rule changes the route and at
  DEBUG when it returns the router's own proposal, so a mostly-agreeing rule adds no
  INFO line per document.
"""
from __future__ import annotations

import functools
import inspect
import threading
import typing
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping

import structlog
from langgraph.graph import END, START

from graph.routing import (
    after_arbiter,
    after_boss,
    after_classification_arbiter,
    after_classify,
    after_extraction_gated,
    after_human_review,
    after_judge,
    after_report,
    after_retry_classify,
    after_retry_extraction_gated,
    after_review_classify,
    entry_route,
)

logger = structlog.get_logger(__name__)


class RouteSpecError(ValueError):
    """The declared routing topology is inconsistent."""


@dataclass(frozen=True)
class RouteRule:
    """An optional refinement of one node's route.

    ``decide(state_view, proposed)`` gets a read-only view of the state and the base
    router's answer, and returns a key of the node's ``destinations`` or ``None``
    (keep ``proposed``). ``name`` identifies the rule in logs and in
    :func:`remove_route_rule`.
    """

    name: str
    decide: Callable[[Mapping[str, Any], str], "str | None"]


@dataclass(frozen=True)
class RouteSpec:
    """One node's outgoing conditional edge.

    ``router`` is the existing base router. ``destinations`` maps each value the
    router may return to a graph node name (or ``END``). ``rules`` are static
    rules that ship with the spec; process-wide rules are registered separately.
    """

    node: str
    router: Callable[[dict], str]
    destinations: Mapping[str, str]
    rules: tuple[RouteRule, ...] = ()

    def __post_init__(self) -> None:
        # Frozen means frozen all the way down: an immutable mapping, a tuple.
        object.__setattr__(self, "destinations", MappingProxyType(dict(self.destinations)))
        object.__setattr__(self, "rules", tuple(self.rules))


# ---------------------------------------------------------------------------
# Declared topology (identical to the edges build_graph() wired by hand)
# ---------------------------------------------------------------------------


def _same(*names: str) -> dict[str, str]:
    """A destination map whose router values are the node names themselves."""
    return {name: name for name in names}


ROUTE_SPECS: tuple[RouteSpec, ...] = (
    RouteSpec(
        START,
        entry_route,
        # compile_report: stateless human-applied review
        _same("intake", "extract", "compile_report"),
    ),
    RouteSpec(
        "classify",
        after_classify,
        # classify: transient-error self-loop; review_classify: GT class miss even at 0.99
        _same("classify", "retry_classify", "review_classify", "extract", "human_review"),
    ),
    RouteSpec(
        "retry_classify",
        after_retry_classify,
        _same("retry_classify", "review_classify", "extract", "human_review"),
    ),
    # KANBAN-062 (Lane A): agent second opinion. High-confidence reviewer -> extract;
    # anything else -> arbiter or human review.
    RouteSpec(
        "review_classify",
        after_review_classify,
        _same("review_classify", "extract", "classification_arbiter", "human_review"),
    ),
    RouteSpec(
        "classification_arbiter",
        after_classification_arbiter,
        _same("extract", "human_review"),
    ),
    RouteSpec(
        "extract",
        after_extraction_gated,
        _same("extract", "retry_extract", "compile_report", "judge_verify", "human_review", "boss_escalation"),
    ),
    RouteSpec(
        "retry_extract",
        after_retry_extraction_gated,
        _same("retry_extract", "compile_report", "judge_verify", "human_review", "boss_escalation"),
    ),
    # KANBAN-063 (Lane B): judge verdict routing, then the arbiter.
    RouteSpec(
        "judge_verify",
        after_judge,
        _same("judge_verify", "compile_report", "arbiter", "human_review"),
    ),
    RouteSpec(
        "arbiter",
        after_arbiter,
        _same("arbiter", "compile_report", "retry_extract", "human_review"),
    ),
    RouteSpec(
        "boss_escalation",
        after_boss,
        _same("boss_escalation", "compile_report", "human_review"),
    ),
    RouteSpec(
        "human_review",
        after_human_review,
        # compile_report: reviewer's fields, no LLM; "failed" ends the run
        {"extract": "extract", "compile_report": "compile_report", "failed": END},
    ),
    RouteSpec(
        "compile_report",
        after_report,
        _same("catalog_write", "human_review"),
    ),
)

# Unconditional edges.
PLAIN_EDGES: tuple[tuple[str, str], ...] = (
    ("intake", "classify"),
    ("catalog_write", "archive"),
    ("archive", END),
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _literal_values(router: Callable[..., Any]) -> "frozenset[str] | None":
    """The ``Literal[...]`` values of ``router``'s return annotation, or ``None``
    when there is no such annotation (or it cannot be resolved)."""
    try:
        hints = typing.get_type_hints(router)
    except Exception as exc:  # unresolved forward reference, odd callable
        logger.warning("route_spec_annotation_unresolved", router=_name(router), error=repr(exc))
        return None
    ret = hints.get("return")
    if typing.get_origin(ret) is not Literal:
        return None
    return frozenset(typing.get_args(ret))


def _name(obj: Any) -> str:
    return getattr(obj, "__name__", repr(obj))


def validate_route_specs(
    specs: Iterable[RouteSpec],
    plain_edges: Iterable[tuple[str, str]],
    nodes: Iterable[str],
) -> None:
    """Raise :class:`RouteSpecError` listing every inconsistency in the topology.

    ``nodes`` are the node names registered on the graph (START and END excluded).
    """
    node_set = set(nodes)
    sources = node_set | {START}
    targets = node_set | {END}
    problems: list[str] = []
    outgoing: dict[str, int] = {}

    for spec in specs:
        outgoing[spec.node] = outgoing.get(spec.node, 0) + 1
        if spec.node not in sources:
            problems.append(f"{spec.node!r}: router {_name(spec.router)} is declared for an unknown node")
        if not spec.destinations:
            problems.append(f"{spec.node!r}: no destinations")
        for value, target in spec.destinations.items():
            if target not in targets:
                problems.append(
                    f"{spec.node!r}: router value {value!r} points at {target!r}, which is not a node or END"
                )
        declared = _literal_values(spec.router)
        if declared is not None and declared != set(spec.destinations):
            missing = sorted(declared - set(spec.destinations))
            extra = sorted(set(spec.destinations) - declared)
            problems.append(
                f"{spec.node!r}: {_name(spec.router)} return annotation and destinations disagree"
                f" (annotated but not mapped: {missing}; mapped but not annotated: {extra})"
            )

    for source, target in plain_edges:
        outgoing[source] = outgoing.get(source, 0) + 1
        if source not in sources:
            problems.append(f"plain edge {source!r} -> {target!r}: unknown source")
        if target not in targets:
            problems.append(f"plain edge {source!r} -> {target!r}: unknown target")

    for node in sorted(node_set):
        count = outgoing.get(node, 0)
        if count == 0:
            problems.append(f"{node!r}: no outgoing edge")
        elif count > 1:
            problems.append(f"{node!r}: {count} outgoing edge definitions (expected exactly one)")
    if outgoing.get(START, 0) != 1:
        problems.append(f"{START!r}: expected exactly one entry route, found {outgoing.get(START, 0)}")

    if problems:
        raise RouteSpecError("invalid route topology:\n  - " + "\n  - ".join(problems))


# ---------------------------------------------------------------------------
# Rule registry (process-wide, copy-on-write: reads take no lock)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_RULES: Mapping[str, tuple[RouteRule, ...]] = MappingProxyType({})
_KNOWN_NODES: frozenset[str] = frozenset(spec.node for spec in ROUTE_SPECS)


def _check_node(node: str) -> None:
    if node not in _KNOWN_NODES:
        raise ValueError(f"no route is declared for node {node!r}; known: {sorted(_KNOWN_NODES)}")


def route_rules(node: str) -> tuple[RouteRule, ...]:
    """The rules currently registered for ``node``, in evaluation order."""
    return _RULES.get(node, ())


def add_route_rule(node: str, rule: RouteRule, *, position: "int | Literal['first', 'last']" = "last") -> None:
    """Register ``rule`` for ``node``'s route.

    ``position`` is ``"last"`` (default), ``"first"`` or an index into the node's
    registered rules. Rule names are unique per node (ValueError otherwise).
    """
    global _RULES
    _check_node(node)
    if inspect.iscoroutinefunction(rule.decide) or inspect.iscoroutinefunction(
        getattr(rule.decide, "__call__", None)
    ):
        raise TypeError(f"route rule {rule.name!r} must be synchronous; async rules are not supported")
    with _LOCK:
        current = list(_RULES.get(node, ()))
        if any(r.name == rule.name for r in current):
            raise ValueError(f"a rule named {rule.name!r} is already registered for {node!r}")
        if position == "last":
            current.append(rule)
        elif position == "first":
            current.insert(0, rule)
        elif isinstance(position, int) and not isinstance(position, bool):
            current.insert(position, rule)
        else:
            raise ValueError(f"position must be 'first', 'last' or an int, got {position!r}")
        _RULES = MappingProxyType({**_RULES, node: tuple(current)})


def replace_route_rules(name_prefix: str, rules: Iterable[tuple[str, RouteRule]]) -> None:
    """Atomically swap every rule whose name starts with ``name_prefix`` for ``rules``.

    ``rules`` are ``(node, rule)`` pairs, appended after the rules that stay. One lock
    acquisition and one registry swap: a concurrent route decision sees either the old
    set or the new one, never a half-installed one. Used by ``graph.decision_points`` to
    (re)install its ``decision_point:*`` rules. Validates like :func:`add_route_rule`
    (known node, synchronous rule, unique name per node) before changing anything.
    """
    global _RULES
    new = list(rules)
    for node, rule in new:
        _check_node(node)
        if inspect.iscoroutinefunction(rule.decide) or inspect.iscoroutinefunction(
            getattr(rule.decide, "__call__", None)
        ):
            raise TypeError(f"route rule {rule.name!r} must be synchronous; async rules are not supported")
    with _LOCK:
        updated: dict[str, list[RouteRule]] = {
            node: [r for r in current if not r.name.startswith(name_prefix)] for node, current in _RULES.items()
        }
        for node, rule in new:
            bucket = updated.setdefault(node, [])
            if any(r.name == rule.name for r in bucket):
                raise ValueError(f"a rule named {rule.name!r} is already registered for {node!r}")
            bucket.append(rule)
        _RULES = MappingProxyType({node: tuple(rs) for node, rs in updated.items() if rs})


def remove_route_rule(node: str, name: str) -> bool:
    """Remove the rule called ``name`` from ``node``. Returns whether one was removed."""
    global _RULES
    with _LOCK:
        current = _RULES.get(node, ())
        kept = tuple(r for r in current if r.name != name)
        if len(kept) == len(current):
            return False
        updated = dict(_RULES)
        if kept:
            updated[node] = kept
        else:
            del updated[node]
        _RULES = MappingProxyType(updated)
        return True


def clear_route_rules(node: "str | None" = None) -> None:
    """Remove every registered rule (of ``node`` only, when given). Tests call this."""
    global _RULES
    with _LOCK:
        if node is None:
            _RULES = MappingProxyType({})
        else:
            _RULES = MappingProxyType({k: v for k, v in _RULES.items() if k != node})


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


_REPR_LIMIT = 200


def _safe_repr(obj: Any) -> str:
    """``repr(obj)`` that cannot raise and cannot flood a log line."""
    try:
        text = repr(obj)
    except Exception:
        return f"<{type(obj).__name__}: repr failed>"
    return text if len(text) <= _REPR_LIMIT else text[:_REPR_LIMIT] + "..."


def decide_route(spec: RouteSpec, state: Mapping[str, Any]) -> str:
    """Decide ``spec``'s route for ``state``: the base router proposes, rules refine.

    The base router is called once, with the original ``state``. Without rules its
    answer is returned. With rules, the first declared destination a rule returns
    wins; otherwise the router's proposal stands.
    """
    proposed = spec.router(state)  # type: ignore[arg-type]  # one call, state untouched
    registered = _RULES.get(spec.node)
    if not spec.rules and not registered:
        return proposed

    view = MappingProxyType(state)
    for rule in (*spec.rules, *(registered or ())):
        try:
            answer = rule.decide(view, proposed)
        except Exception as exc:
            logger.warning(
                "route_rule_failed",
                node=spec.node,
                rule=rule.name,
                error=_safe_repr(exc),
                error_type=type(exc).__name__,
            )
            continue
        if answer is None:
            continue
        try:
            # Canonicalise str subclasses to an exact str, so a hostile __hash__ /
            # __eq__ cannot pass this check and then fail in LangGraph's branch map.
            if isinstance(answer, str) and type(answer) is not str:
                answer = str.__str__(answer)
            valid = isinstance(answer, str) and answer in spec.destinations
            changed = valid and answer != proposed
        except Exception:  # e.g. a str subclass whose __hash__ / __eq__ raises
            valid = changed = False
        if not valid:
            logger.warning(
                "route_rule_unknown_destination",
                node=spec.node,
                rule=rule.name,
                answer=_safe_repr(answer),
                answer_type=type(answer).__name__,
                allowed=sorted(spec.destinations),
            )
            continue
        (logger.info if changed else logger.debug)(
            "route_rule_applied",
            node=spec.node,
            rule=rule.name,
            destination=answer,
            proposed=_safe_repr(proposed),
            overrode=changed,
        )
        return answer
    return proposed


def edge_router(spec: RouteSpec) -> Callable[[dict], str]:
    """The callable handed to ``add_conditional_edges`` for ``spec``.

    It keeps the base router's ``__name__`` (LangGraph names the branch after it,
    and the compiled topology must not change).
    """

    @functools.wraps(spec.router)
    def route(state: dict) -> str:
        return decide_route(spec, state)

    return route


def wire_edges(
    workflow: Any,
    specs: Iterable[RouteSpec] = ROUTE_SPECS,
    plain_edges: Iterable[tuple[str, str]] = PLAIN_EDGES,
) -> None:
    """Validate the topology against the nodes already added to ``workflow`` and
    add every edge. Raises :class:`RouteSpecError` before adding any edge."""
    specs = tuple(specs)
    plain_edges = tuple(plain_edges)
    validate_route_specs(specs, plain_edges, nodes=workflow.nodes)
    for spec in specs:
        workflow.add_conditional_edges(spec.node, edge_router(spec), dict(spec.destinations))
    for source, target in plain_edges:
        workflow.add_edge(source, target)
