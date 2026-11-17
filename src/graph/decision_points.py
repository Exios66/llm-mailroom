"""Pluggable decision points: a decision model that refines a node's route.

A *slot* (taxonomy ``decision_points:``) names a graph node and a backend. Right after
the node returns, the slot's backend is consulted with the node's result and answers with
a :class:`DecisionOutcome`: a label, a confidence, review causes and an optional
``route_hint``. The outcome is written to ``state["decision_outcomes"][slot]``; a
:class:`~graph.route_spec.RouteRule` named ``decision_point:<slot>`` then reads it when the
node's route is decided and may redirect the document. The router still decides first.

Default: ``decision_points: {}``. With no slot configured nothing here is installed: no
node is wrapped, no rule is registered, ``decision_outcomes`` never appears in state.

How the pieces fit (``build_graph()`` calls :func:`prepare_slots`, :func:`wrap_node`,
:func:`wrap_human_review` and :func:`install_rules`):

* **Backends** are plain callables ``fn(DecisionInput) -> DecisionOutcome | Mapping``,
  registered process-wide by name (:func:`register_decision_point_backend`). The graph
  never constructs one; the host (the sandbox, an eval, a test) registers it.
* **Consultation** (:func:`consult`) runs the backend on a daemon thread bounded by the
  slot's wall-clock timeout and the run deadline. It never raises into the graph: a
  timeout, a transient provider error (``llm.retry.is_transient_error``), a missing
  backend or a refused call (too many stalled calls) is ``unavailable``; any other
  exception or a malformed answer is ``invalid``. ``on_failure`` decides what a failure
  means: ``fail_open`` gives no hint, ``fail_safe`` hints ``human_review`` with the cause
  ``decision_point_unavailable`` / ``decision_point_invalid``.
* **Policy** (:func:`vet_outcome`) decides whether a hint may be followed: the outcome is
  ``ok``, the hint is in the slot's ``allow`` (per class: ``by_class``), its confidence
  meets ``min_confidence`` when set, and a hint to ``human_review`` carries causes. The
  rule additionally never overrides the router's own ``human_review`` park, a
  self-loop of the node, or a proposed verification gate (:data:`GATE_PROPOSALS`:
  ``retry_extract``, ``judge_verify``, ``arbiter``, ``boss_escalation``): those carry the
  pipeline's safety budgets and guarantees (see ``route_spec``'s rule semantics). On the
  extraction side a hint can add scrutiny, never remove it; classification routing
  (``retry_classify``, ``review_classify``, ``classification_arbiter``) stays open to hints
  as the slot's ``allow`` says, and the extraction gates still run afterwards.
* **Staleness.** The wrapper rewrites the slot's entry on every pass of its node, and
  drops it when the node asked for a transient retry (nothing is consulted: the node's
  result is not usable and will be redone, and the router will retry or park it). A person
  resolving a park clears every outcome (:func:`wrap_human_review`), so an answer about an
  earlier attempt can never route a later one.
* **Override budget.** A hint to a destination other than ``human_review`` that replaces the
  router's proposal spends one of the slot's ``max_overrides`` (default 1) for this document;
  the count lives in ``state["decision_override_counts"]`` and is never reset within a run.
  Past the budget the outcome is stored ``capped`` and the rule keeps the router's
  proposal (logged as ``decision_point_override_capped``), so a cycle of hints across slots
  (``arbiter`` to ``retry_extract`` to ``judge_verify`` to ``arbiter``) ends whatever the
  topology. The budgets of the active slots that can redirect (``SlotPolicy.can_redirect``) may add
  up to ``MAX_TOTAL_OVERRIDES`` (8), the global cap on redirects per document; config
  validation enforces it. With several slots on one node the rules run in config order and
  the first answer wins, so only the slot that actually answers is charged. Parks are
  exempt (they need a person and ``fail_safe`` must keep parking).
* **Backends.** ``backend:`` is a registered name or an import spec ``module.path:callable``.
  ``prepare_slots`` (called at startup by ``Watcher.start()`` and the API lifespan, and by
  ``build_graph()``) imports a spec and registers it under that string; an import or
  attribute error, or an async callable, is a ``DecisionPointConfigError``. A bare name that
  is not registered yet only logs ``decision_point_backend_unregistered`` (slot, backend,
  ``on_failure``; once per slot per process): registering lazily stays allowed.
* **Limits of this phase.** The stalled-thread cap (``_MAX_ABANDONED_DECISION_POINT_THREADS``:
  timed-out calls whose thread still runs; healthy in-flight calls never count) is one list
  shared by every slot, so four stalled calls make every slot's calls refuse
  (``unavailable``) until one finishes. A consultation runs inside the node's ``_bounded`` wrapper, after the
  node itself: its time (the slot's ``timeout_seconds``, at most the time left on the run
  deadline) counts toward the node's wall-clock budget, not a separate one. There is no
  circuit breaker: every document consults its slot, and a failing
  backend is retried by the next document, so a slow or down backend costs up to
  ``timeout_seconds`` per document per slot. A circuit breaker is planned for a later phase.
* **Review causes.** An outcome whose vetted route is a park shows up in
  ``pipeline.reconsideration.collect_review_causes`` as
  ``decision_point:<slot>:<cause>``.

The older one-slot registries (``agents.decision_backend`` for the classification
reviewer, ``agents.extraction_gate`` for the extraction gate) are unchanged and do not use
slots yet; they will be moved onto slots later.

Never logged: document text, evidence, backend error messages, the backend's ``label``
(free text that could quote the document). Spans and logs carry the slot, backend, node,
status, route hint, the label's length, confidence, cause tokens and the exception class
name.
"""
from __future__ import annotations

import contextvars
import copy
import functools
import itertools
import inspect
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

import structlog
from langgraph.graph import START

from graph.route_spec import (
    ROUTE_SPECS,
    RouteRule,
    RouteSpec,
    replace_route_rules,
)
from observability.tracing import observation
from pipeline.config import (
    DecisionPointConfigError,
    get_decision_points_config,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "DecisionBackendTimeout",
    "DecisionInput",
    "DecisionOutcome",
    "DecisionPointConfigError",
    "RULE_PREFIX",
    "SlotPolicy",
    "build_handoff",
    "clear_decision_point_backends",
    "clear_rules",
    "consult",
    "get_decision_point_backend",
    "install_rules",
    "load_slot_policies",
    "normalize_outcome",
    "prepare_slots",
    "register_decision_point_backend",
    "rule_for",
    "run_bounded",
    "unregister_decision_point_backend",
    "validate_slot_policies",
    "vet_outcome",
    "wrap_human_review",
    "wrap_node",
]

PARK = "human_review"
#: Router proposals a hint never overrides besides a park and a self-loop: each is a gate a
#: document must pass before it may be archived. ``retry_extract`` redoes a lost window, an
#: invalid schema or a hollow extraction; ``judge_verify`` and ``arbiter`` verify an unsure
#: or partial extraction; ``boss_escalation`` adjudicates a matter conflict. A hint may add
#: scrutiny (a hint *to* one of these is allowed), never remove it. Classification routes
#: are deliberately not gated: a slot may skip a classification retry or review when its
#: ``allow`` says so, since the extraction gates still run afterwards.
GATE_PROPOSALS = frozenset({"retry_extract", "judge_verify", "arbiter", "boss_escalation"})
RULE_PREFIX = "decision_point:"
OUTCOMES_KEY = "decision_outcomes"
OVERRIDES_KEY = "decision_override_counts"
# Upper bound on the summed ``max_overrides`` of the active slots: a document is redirected
# by decision points at most this many times in total, whatever the topology. (Without a
# budget a hint cycle only ends at the run's token budget or LangGraph's recursion limit,
# which is 10007 steps by default in the installed version.)
MAX_TOTAL_OVERRIDES = 8

Status = Literal["ok", "unavailable", "invalid"]
_STATUSES = ("ok", "unavailable", "invalid")


# ---------------------------------------------------------------------------
# Bounded calls (shared with graph.build_graph._run_bounded)
# ---------------------------------------------------------------------------


class DecisionBackendTimeout(Exception):
    """A decision backend did not answer within the call/run deadline."""


def run_bounded(
    call: Callable[[], Any],
    *,
    name: str,
    abandoned: list[threading.Thread],
    max_abandoned: int,
    lock: threading.Lock,
    timeout: "float | None" = None,
) -> Any:
    """Run ``call()`` with a wall-clock bound and return its value.

    The timeout is ``timeout`` (default ``llm_call_timeout_seconds``), shortened to the
    time left on the run deadline (a huge or NaN value is clamped or refused, never an
    ``OverflowError``). The call runs on a daemon thread so a stalled backend can neither
    block the worker nor keep the process alive; on expiry :class:`DecisionBackendTimeout`
    is raised and the stray thread stays tracked in ``abandoned``.

    ``abandoned`` holds the timed-out threads of this kind that are still running. At
    ``max_abandoned`` of them, new calls are refused with :class:`DecisionBackendTimeout`
    instead of starting another. Healthy in-flight calls do not count, so concurrent
    documents are never refused for being busy. The cap check and the thread start happen
    under ``lock``, and a timed-out thread is added under it too; calls already in flight
    when the cap is reached can still time out and join the list, so the list can exceed
    the cap by at most the number of concurrent callers. Exceptions raised by ``call``
    propagate unchanged. The caller's contextvars (run deadline, token accounting,
    tracing) are visible to ``call``.
    """
    from pipeline import limits

    if timeout is None:
        timeout = limits.get_call_timeout_seconds()
    try:
        timeout = float(timeout)
    except OverflowError:  # an int too large for a float: as good as no limit
        timeout = float(threading.TIMEOUT_MAX)
    deadline = limits.get_run_deadline()
    if deadline is not None:
        timeout = min(timeout, deadline - time.time())
    if not timeout > 0:  # also NaN
        raise DecisionBackendTimeout("no time left on the run deadline")
    timeout = min(timeout, float(threading.TIMEOUT_MAX))

    box: dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def _target() -> None:
        """Run the call on this thread and hand its result or error back."""
        try:
            box["value"] = ctx.run(call)
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name=name, daemon=True)
    with lock:  # check the cap and start in one step
        abandoned[:] = [t for t in abandoned if t.is_alive()]
        if len(abandoned) >= max_abandoned:
            raise DecisionBackendTimeout(f"{len(abandoned)} stalled {name} calls still running")
        try:
            thread.start()
        except RuntimeError:  # cannot start a new thread: no answer, not a backend fault
            raise DecisionBackendTimeout(f"could not start a {name} thread") from None
    try:
        thread.join(timeout)
    except OverflowError:  # belt and braces: the clamp above should make this unreachable
        thread.join(float(threading.TIMEOUT_MAX))
    if thread.is_alive():
        with lock:
            abandoned.append(thread)
        raise DecisionBackendTimeout(f"{name} exceeded {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# Timed-out decision-point calls whose daemon thread is still running. At this many, new
# calls are refused (the slot's on_failure applies). Own list and
# cap: stalled slots can never make the classification reviewer or the extraction gate
# refuse calls, and the reverse.
_MAX_ABANDONED_DECISION_POINT_THREADS = 4
_abandoned_threads: list[threading.Thread] = []
_abandoned_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Contract: input and outcome
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")  # used with fullmatch: "$" would accept a trailing newline
_MAX_CAUSES = 8
_MAX_TEXT = 200
_MAX_EVIDENCE_BYTES = 8192


def _exact_str(value: Any) -> Any:
    """A ``str`` subclass instance as a plain ``str`` (``__eq__``, ``__hash__``, ``__len__``
    and friends of a hostile subclass no longer decide anything); other values as they are."""
    return str.__str__(value) if isinstance(value, str) else value


def _unit_float(value: Any) -> bool:
    """Finite number in [0, 1], booleans excluded. Never raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0.0 <= value <= 1.0 and math.isfinite(value)  # bounds first: isfinite raises on huge ints


def _isolated(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy of ``mapping``, key by key; a value that cannot be copied is dropped."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        try:
            out[key] = copy.deepcopy(value)
        except Exception:
            logger.debug("decision_point_input_value_not_copied", key=str(key)[:60])
    return out


@dataclass(frozen=True)
class DecisionInput:
    """What a backend sees for one consultation (read-only).

    ``doc_text`` is the document's full text, never truncated: a backend that has a
    context limit windows it itself. ``handoff`` is a small dict of the node's key
    outputs; ``state`` is a read-only view of the state after the node ran. Both are deep
    copies, so a backend cannot change the graph's state through them (a value that cannot
    be copied is left out). ``repr`` shows no document content.
    """

    slot: str
    node: str
    doc_id: "str | None"
    doc_type: "str | None"
    doc_subclass: "str | None"
    doc_text: str = field(repr=False)
    handoff: Mapping[str, Any] = field(repr=False, default_factory=dict)
    state: Mapping[str, Any] = field(repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        # Deep copies: a backend runs on its own thread (and may outlive its timeout), so it
        # must never hold a live object of the graph's state. A value that cannot be copied
        # is left out of the view rather than shared.
        object.__setattr__(self, "handoff", MappingProxyType(_isolated(self.handoff)))
        object.__setattr__(self, "state", MappingProxyType(_isolated(self.state)))


class OutcomeValidationError(ValueError):
    """Raised by :class:`DecisionOutcome` validation (a ``ValueError`` for existing callers).

    A backend can import and raise it too, so its type does not prove the text is ours:
    :func:`_safe_detail` echoes only the exact messages in :data:`_VALIDATION_MESSAGES` and
    records anything else, this class included, by class name.
    """


@dataclass(frozen=True)
class DecisionOutcome:
    """A decision backend's answer, validated at construction.

    ``route_hint`` is a destination the backend would like the document to take (the
    slot's policy decides whether it may); ``label`` a short verdict; ``confidence``
    finite in [0, 1] or ``None``; ``causes`` machine tokens (``[A-Za-z0-9_.:-]``, at most
    64 characters, at most 8) that become review causes when the hint parks the
    document; ``model`` the pinned model version; ``evidence`` JSON-serialisable detail
    (at most 8 KB serialised), kept on state and never logged. ``status`` is ``ok`` for
    an answer; ``unavailable`` and ``invalid`` are what :func:`consult` records when
    there is none. Violations raise ``ValueError`` here; :func:`normalize_outcome`
    turns them into an ``invalid`` outcome instead.
    """

    route_hint: "str | None" = None
    label: "str | None" = None
    confidence: "float | None" = None
    causes: tuple[str, ...] = ()
    model: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    status: Status = "ok"

    def __post_init__(self) -> None:
        # Every text field is coerced to an exact ``str`` first, so what is validated is what
        # is stored and compared later (a ``str`` subclass could answer "equal" to anything).
        hint = _exact_str(self.route_hint)
        if hint is not None and (not isinstance(hint, str) or not hint or len(hint) > 64):
            raise OutcomeValidationError("route_hint must be a destination name or None")
        object.__setattr__(self, "route_hint", hint)
        label = _exact_str(self.label)
        if label is not None and (not isinstance(label, str) or len(label) > _MAX_TEXT):
            raise OutcomeValidationError("label must be a string of at most 200 characters or None")
        object.__setattr__(self, "label", label)
        conf = self.confidence
        if conf is not None:
            if isinstance(conf, float):
                conf = float.__float__(conf)  # an exact float, whatever a subclass overrides
            elif isinstance(conf, int) and not isinstance(conf, bool):
                conf = int.__int__(conf)
            if not _unit_float(conf):
                raise OutcomeValidationError("confidence must be a finite number from 0 to 1 or None")
            object.__setattr__(self, "confidence", float(conf))
        causes = self.causes
        if isinstance(causes, (str, bytes)) or not isinstance(causes, Iterable):
            raise OutcomeValidationError("causes must be a sequence of tokens")
        # Read at most one more than the cap, so an endless iterator cannot hang us.
        items = list(itertools.islice(causes, _MAX_CAUSES + 1))
        if len(items) > _MAX_CAUSES or not all(isinstance(t, str) for t in items):
            raise OutcomeValidationError("causes must be at most 8 tokens of [A-Za-z0-9_.:-], 1 to 64 characters")
        tokens = tuple(dict.fromkeys(_exact_str(t) for t in items))
        if not all(_TOKEN.fullmatch(t) for t in tokens):
            raise OutcomeValidationError("causes must be at most 8 tokens of [A-Za-z0-9_.:-], 1 to 64 characters")
        object.__setattr__(self, "causes", tokens)
        model = _exact_str(self.model)
        if not isinstance(model, str) or len(model) > _MAX_TEXT:
            raise OutcomeValidationError("model must be a string of at most 200 characters")
        object.__setattr__(self, "model", model)
        if not isinstance(self.evidence, Mapping) or not all(isinstance(k, str) for k in self.evidence):
            raise OutcomeValidationError("evidence must be a mapping with string keys")
        try:
            encoded = json.dumps(dict(self.evidence), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise OutcomeValidationError("evidence must be JSON-serialisable") from None
        if len(encoded) > _MAX_EVIDENCE_BYTES:
            raise OutcomeValidationError("evidence is larger than 8 KB")
        object.__setattr__(self, "evidence", json.loads(encoded))
        status = _exact_str(self.status)
        if not isinstance(status, str) or status not in _STATUSES:
            raise OutcomeValidationError("status must be ok, unavailable or invalid")
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (what lands in ``decision_outcomes``)."""
        return {
            "route_hint": self.route_hint,
            "label": self.label,
            "confidence": self.confidence,
            "causes": list(self.causes),
            "model": self.model,
            "evidence": json.loads(json.dumps(dict(self.evidence))),
            "status": self.status,
        }


_OUTCOME_KEYS = ("route_hint", "label", "confidence", "causes", "model", "evidence", "status")


def _failure(status: Status, reason: str, **extra: Any) -> DecisionOutcome:
    return DecisionOutcome(status=status, evidence={"reason": reason, **extra})


def normalize_outcome(raw: Any) -> DecisionOutcome:
    """A backend's return value as a plain :class:`DecisionOutcome`; anything malformed is ``invalid``.

    Accepts an outcome or a mapping with any of the outcome's keys (unknown keys are
    dropped). Every :class:`DecisionOutcome`, a subclass or a plain one whose ``evidence``
    the backend mutated after construction, is rebuilt field by field and so validated like
    any other answer. Never raises. It reads the backend's object (a lazy
    mapping, an iterator), so :func:`consult` runs it inside the bounded call.
    """
    return _normalize_answer(raw)[0]


def _normalize_answer(raw: Any) -> "tuple[DecisionOutcome, bool]":
    """:func:`normalize_outcome`, plus whether the outcome is our own ``malformed_answer`` verdict.

    The flag is what lets :func:`consult` keep our evidence and drop the backend's: a backend
    can return ``status="invalid"`` with an evidence ``reason`` that imitates ours.
    """
    try:
        if isinstance(raw, DecisionOutcome):
            # Always rebuilt, even an exact DecisionOutcome: its ``evidence`` dict stays
            # mutable after validation, and the backend still holds it.
            return DecisionOutcome(**{k: getattr(raw, k) for k in _OUTCOME_KEYS}), False
        if not isinstance(raw, Mapping):
            return _failure("invalid", "malformed_answer", answer_type=type(raw).__name__), True
        return DecisionOutcome(**{k: raw[k] for k in _OUTCOME_KEYS if k in raw}), False
    except Exception as exc:  # ValueError from validation; anything a hostile object raises
        return _failure("invalid", "malformed_answer", detail=_safe_detail(exc)), True


#: Every message ``DecisionOutcome`` validation raises (pinned by a test). A backend can
#: import and raise ``OutcomeValidationError`` itself, so the class alone is not proof the
#: text is ours: only these exact messages are echoed.
_VALIDATION_MESSAGES = frozenset({
    "causes must be a sequence of tokens",
    "causes must be at most 8 tokens of [A-Za-z0-9_.:-], 1 to 64 characters",
    "confidence must be a finite number from 0 to 1 or None",
    "evidence is larger than 8 KB",
    "evidence must be JSON-serialisable",
    "evidence must be a mapping with string keys",
    "label must be a string of at most 200 characters or None",
    "model must be a string of at most 200 characters",
    "route_hint must be a destination name or None",
    "status must be ok, unavailable or invalid",
})


def _safe_detail(exc: BaseException) -> str:
    """Our own validation message, or the exception class: never a backend-supplied value."""
    args = exc.args if type(exc) is OutcomeValidationError else ()
    if len(args) == 1 and type(args[0]) is str and args[0] in _VALIDATION_MESSAGES:
        return args[0]
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Registry of backends (process-wide, copy-on-write: reads take no lock)
# ---------------------------------------------------------------------------

DecisionPointBackend = Callable[[DecisionInput], "DecisionOutcome | Mapping[str, Any]"]

_BACKEND_LOCK = threading.Lock()
_BACKENDS: Mapping[str, DecisionPointBackend] = MappingProxyType({})


def _is_async(fn: Any) -> bool:
    """A coroutine function, or an object whose ``__call__`` is one (as ``add_route_rule`` checks)."""
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))


def register_decision_point_backend(name: str, backend: DecisionPointBackend) -> None:
    """Install ``backend`` under ``name`` process-wide; a second registration replaces it."""
    global _BACKENDS
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a decision point backend needs a non-empty name")
    if not callable(backend):
        raise TypeError(f"decision point backend {name!r} must be callable")
    if _is_async(backend):
        raise TypeError(f"decision point backend {name!r} must be synchronous; async backends are not supported")
    with _BACKEND_LOCK:
        _BACKENDS = MappingProxyType({**_BACKENDS, name: backend})


def get_decision_point_backend(name: str) -> "DecisionPointBackend | None":
    """The backend registered as ``name``, or ``None``."""
    return _BACKENDS.get(name)


def unregister_decision_point_backend(name: str) -> bool:
    """Remove ``name``. Returns whether one was removed."""
    global _BACKENDS
    with _BACKEND_LOCK:
        if name not in _BACKENDS:
            return False
        _BACKENDS = MappingProxyType({k: v for k, v in _BACKENDS.items() if k != name})
        return True


def clear_decision_point_backends() -> None:
    """Remove every backend (tests)."""
    global _BACKENDS
    with _BACKEND_LOCK:
        _BACKENDS = MappingProxyType({})


# ---------------------------------------------------------------------------
# Slot policy (config -> frozen objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClassOverride:
    enabled: "bool | None" = None
    allow: "tuple[str, ...] | None" = None
    min_confidence: "float | None" = None


@dataclass(frozen=True)
class _Effective:
    enabled: bool
    allow: frozenset
    min_confidence: "float | None"


@dataclass(frozen=True)
class SlotPolicy:
    """One configured slot (see ``taxonomy.yaml`` ``decision_points:``)."""

    slot: str
    node: str
    backend: str
    enabled: bool = True
    timeout_seconds: "float | None" = None
    on_failure: Literal["fail_open", "fail_safe"] = "fail_open"
    allow: tuple[str, ...] = ()
    min_confidence: "float | None" = None
    by_class: Mapping[str, _ClassOverride] = field(default_factory=dict)
    max_overrides: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_class", MappingProxyType(dict(self.by_class)))

    def resolve(self, doc_type: Any) -> _Effective:
        """enabled / allow / min_confidence for a document class (``by_class`` wins)."""
        override = self.by_class.get(doc_type) if isinstance(doc_type, str) else None
        if override is None:
            return _Effective(self.enabled, frozenset(self.allow), self.min_confidence)
        return _Effective(
            self.enabled if override.enabled is None else override.enabled,
            frozenset(self.allow if override.allow is None else override.allow),
            self.min_confidence if override.min_confidence is None else override.min_confidence,
        )

    @property
    def can_redirect(self) -> bool:
        """Whether some class can be sent to a destination other than ``human_review``.

        False for a record-only slot (empty ``allow`` for every class) and for one that may
        only park: ``max_overrides`` never matters to it.
        """
        # Only the allow lists that are in force count: the slot-wide one when the slot is
        # enabled (it covers every class without an override), and each enabled class's own.
        lists = [frozenset(self.allow)] if self.enabled else []
        for doc_type in self.by_class:
            effective = self.resolve(doc_type)
            if effective.enabled:
                lists.append(effective.allow)
        return any(dest != PARK for dests in lists for dest in dests)

    def loggable_hint(self, hint: Any) -> "str | None":
        """``hint`` when it names a destination this slot configures (or the park), else a marker.

        A backend's ``route_hint`` is free text up to 64 characters and could quote the
        document; logs and traces only ever carry a name the operator wrote in taxonomy.
        """
        if hint is None:
            return None
        known = {PARK, *self.allow}
        for override in self.by_class.values():
            known.update(override.allow or ())
        return hint if hint in known else "<unlisted>"

    @property
    def active(self) -> bool:
        """On for some class: the slot itself, or a ``by_class`` entry that enables it."""
        return self.enabled or any(o.enabled for o in self.by_class.values())


def load_slot_policies(config: "Mapping[str, Mapping[str, Any]] | None" = None) -> tuple[SlotPolicy, ...]:
    """The configured slots, in file order. ``config`` is a normalised
    :func:`pipeline.config.get_decision_points_config` result (default: the taxonomy)."""
    cfg = get_decision_points_config() if config is None else config
    policies = []
    for slot, entry in cfg.items():
        policies.append(
            SlotPolicy(
                slot=slot,
                node=entry["node"],
                backend=entry["backend"],
                enabled=entry["enabled"],
                timeout_seconds=entry["timeout_seconds"],
                on_failure=entry["on_failure"],
                allow=tuple(entry["allow"]),
                min_confidence=entry["min_confidence"],
                max_overrides=entry["max_overrides"],
                by_class={
                    cls: _ClassOverride(
                        enabled=o.get("enabled"),
                        allow=tuple(o["allow"]) if "allow" in o else None,
                        min_confidence=o.get("min_confidence"),
                    )
                    for cls, o in entry["by_class"].items()
                },
            )
        )
    return tuple(policies)


def validate_slot_policies(
    policies: Iterable[SlotPolicy], specs: Iterable[RouteSpec] = ROUTE_SPECS
) -> None:
    """Raise :class:`DecisionPointConfigError` listing every way a slot disagrees with the graph.

    A slot's node must have a conditional route (``intake``, ``catalog_write`` and
    ``archive`` have none) and must not be ``human_review`` (a person's decision is not
    refined by a model) or ``START``. Every ``allow`` entry (slot and per class) must be
    a destination of that node's route, and never the node itself (a self-loop is the
    transient-retry budget). ``fail_safe`` needs ``human_review`` among the destinations.
    """
    policies = tuple(policies)
    by_node = {spec.node: spec for spec in specs}
    eligible = sorted(n for n in by_node if n not in (START, PARK))
    problems: list[str] = []
    for policy in policies:
        where = f"decision_points.{policy.slot}"
        spec = by_node.get(policy.node)
        if policy.node in (START, PARK):
            why = "a person's decision is final" if policy.node == PARK else "it is not a node"
            problems.append(f"{where}: node {policy.node!r} cannot carry a decision point ({why})")
            continue
        if spec is None:
            problems.append(
                f"{where}: node {policy.node!r} has no conditional route to refine; choose one of {eligible}"
            )
            continue
        allowed = sorted(spec.destinations)
        lists = [("allow", policy.allow)] + [
            (f"by_class.{cls}.allow", o.allow) for cls, o in policy.by_class.items() if o.allow is not None
        ]
        for label, dests in lists:
            for dest in dests:
                if dest == policy.node:
                    problems.append(
                        f"{where}: {label} lists {dest!r}, the node itself; a self-loop is the "
                        f"transient-retry budget and a hint may not choose it"
                    )
                elif dest not in spec.destinations:
                    problems.append(
                        f"{where}: {label} lists {dest!r}, which is not a destination of {policy.node!r}; "
                        f"choose from {allowed}"
                    )
        if policy.on_failure == "fail_safe" and PARK not in spec.destinations:
            problems.append(f"{where}: on_failure fail_safe needs {PARK!r} among {policy.node!r}'s destinations")
    total = sum(p.max_overrides for p in policies if p.active and p.can_redirect)
    if total > MAX_TOTAL_OVERRIDES:
        problems.append(
            f"decision_points: the max_overrides of the active slots that can redirect add up to {total}; "
            f"at most {MAX_TOTAL_OVERRIDES} redirects per document are allowed in total (each is an extra "
            f"graph step). Lower them, or set `max_overrides: 0` on a slot that should only record or park"
        )
    if problems:
        raise DecisionPointConfigError("invalid decision_points config:\n  - " + "\n  - ".join(problems))


# Warnings that would otherwise repeat on every build or every document: once per process.
_warned_unregistered: set = set()
_warned_missing: set = set()
_warn_lock = threading.Lock()


def _first_time(seen: set, key: Any) -> bool:
    """True the first time ``key`` is offered (thread-safe)."""
    with _warn_lock:
        if key in seen:
            return False
        seen.add(key)
        return True


_IMPORT_SPEC = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*\Z")


def _resolve_backend(policy: SlotPolicy) -> None:
    """Make ``policy.backend`` resolvable at consult time, or say why it will not be.

    ``module.path:callable`` is imported now (once per build) and registered under that
    exact string; an import or attribute error, a malformed spec or a non-callable raises
    :class:`DecisionPointConfigError`. A bare name is looked up in the registry: one that
    is not registered yet only logs ``decision_point_backend_unregistered`` (once per slot
    per process; registering lazily, after the build, stays allowed; until then the slot's
    ``on_failure`` applies).
    The module's own import-time code runs here, so the spec is as trusted as the taxonomy.
    """
    import importlib

    spec = policy.backend
    where = f"decision_points.{policy.slot}"
    if ":" not in spec:
        if get_decision_point_backend(spec) is None and _first_time(
            _warned_unregistered, (policy.slot, spec, policy.on_failure)
        ):
            logger.warning(
                "decision_point_backend_unregistered",
                slot=policy.slot, backend=spec, node=policy.node, on_failure=policy.on_failure,
            )
        return
    if not _IMPORT_SPEC.match(spec):
        raise DecisionPointConfigError(
            f"invalid decision_points config:\n  - {where}: backend {spec!r} is not a registered name "
            f"or an import spec 'module.path:callable'"
        )
    module_name, _, attr_path = spec.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
        for part in attr_path.split("."):
            target = getattr(target, part)
    except (Exception, SystemExit) as exc:  # ImportError, AttributeError, a module that exits, ...
        raise DecisionPointConfigError(
            f"invalid decision_points config:\n  - {where}: cannot import backend {spec!r}: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        ) from exc
    if not callable(target):
        raise DecisionPointConfigError(
            f"invalid decision_points config:\n  - {where}: backend {spec!r} is not callable"
        )
    if _is_async(target):
        raise DecisionPointConfigError(
            f"invalid decision_points config:\n  - {where}: backend {spec!r} is async; backends run "
            f"synchronously on a worker thread, so a coroutine function would never execute"
        )
    register_decision_point_backend(spec, target)


def prepare_slots(specs: Iterable[RouteSpec] = ROUTE_SPECS) -> tuple[SlotPolicy, ...]:
    """Load, validate and return the slots that are on for some class (``()`` by default).

    Raises :class:`DecisionPointConfigError`. Called by ``Watcher.start()`` and the API
    lifespan (next to ``verify_specialist_pins``), so a bad section stops the process at
    startup, and again by ``build_graph()`` (the graph is built lazily, on the first
    document, and tests build it directly). Each active slot's
    backend is resolved here (:func:`_resolve_backend`): import specs are imported and
    registered, unregistered bare names are warned about.
    """
    policies = load_slot_policies()
    validate_slot_policies(policies, specs)
    active = tuple(p for p in policies if p.active)
    for policy in active:
        _resolve_backend(policy)
    return active


# ---------------------------------------------------------------------------
# Policy: may this outcome's hint be followed?
# ---------------------------------------------------------------------------

_FAILURE_CAUSE = {"unavailable": "decision_point_unavailable", "invalid": "decision_point_invalid"}


def vet_outcome(policy: SlotPolicy, record: Mapping[str, Any], doc_type: Any = None) -> "str | None":
    """The route ``record`` (a stored outcome dict) may request under ``policy``, else ``None``.

    ``ok``: the hint must be in ``allow`` (for ``doc_type``), meet ``min_confidence`` when
    set (a missing confidence fails it), and, for ``human_review``, carry causes. A failed
    consultation (``unavailable`` / ``invalid``) requests ``human_review`` only under
    ``on_failure: fail_safe``, with the causes :func:`consult` recorded; ``allow`` does not
    gate that. Pure; tolerates a malformed record (returns ``None``).
    """
    try:
        effective = policy.resolve(doc_type)
        if not effective.enabled:
            return None
        hint = record.get("route_hint")
        status = record.get("status")
        causes = record.get("causes")
        has_causes = isinstance(causes, (list, tuple)) and any(isinstance(c, str) and c for c in causes)
        if not isinstance(hint, str):
            return None
        if status == "ok":
            if hint not in effective.allow:
                return None
            if effective.min_confidence is not None:
                confidence = record.get("confidence")
                if not _unit_float(confidence) or confidence < effective.min_confidence:
                    return None
            if hint == PARK and not has_causes:
                return None
            return hint
        if status in _FAILURE_CAUSE and policy.on_failure == "fail_safe":
            return PARK if hint == PARK and has_causes else None
        return None
    except Exception:  # a hostile record must never break routing
        return None


def rule_for(policy: SlotPolicy) -> RouteRule:
    """The :class:`RouteRule` that applies ``policy``'s stored outcome to its node's route.

    Pure and synchronous: it reads ``state["decision_outcomes"][slot]`` (written by the
    node wrapper) and the policy, nothing else. It keeps the router's proposal when that
    is a park (``human_review``), a self-loop of the node or a verification gate
    (:data:`GATE_PROPOSALS`).
    """

    def decide(state: Mapping[str, Any], proposed: str) -> "str | None":
        if proposed == PARK or proposed == policy.node or proposed in GATE_PROPOSALS:
            return None  # parks, transient retries and verification gates are kept
        outcomes = state.get(OUTCOMES_KEY)
        record = outcomes.get(policy.slot) if isinstance(outcomes, Mapping) else None
        if not isinstance(record, Mapping):
            return None
        if record.get("shadowed"):
            return None  # not charged when stored, so never applied: accounting and routing agree
        hint = vet_outcome(policy, record, state.get("doc_type"))
        if hint is None or hint == proposed:
            return None
        if record.get("capped"):
            counts = state.get(OVERRIDES_KEY)
            logger.warning(
                "decision_point_override_capped",
                slot=policy.slot, node=policy.node, hint=hint, proposed=proposed,
                max_overrides=policy.max_overrides,
                overrides=counts.get(policy.slot) if isinstance(counts, Mapping) else None,
            )
            return None
        return hint

    return RouteRule(name=f"{RULE_PREFIX}{policy.slot}", decide=decide)


def clear_rules() -> None:
    """Remove every ``decision_point:*`` rule from every node (idempotent, atomic)."""
    replace_route_rules(RULE_PREFIX, ())


def install_rules(policies: Iterable[SlotPolicy]) -> None:
    """Make the registered ``decision_point:*`` rules exactly those of ``policies``.

    One atomic swap (``route_spec.replace_route_rules``): a route decision running on
    another thread sees the old set or the new one, never none in between. Idempotent:
    stale rules (an earlier build, another config) are replaced. Rules are process-wide,
    like every route rule, so the latest ``build_graph()`` wins; one process runs one
    configuration.
    """
    replace_route_rules(RULE_PREFIX, [(p.node, rule_for(p)) for p in policies])


# ---------------------------------------------------------------------------
# Consultation
# ---------------------------------------------------------------------------

_HANDOFF_SCALARS = (
    "classification_confidence", "classification_attempts", "extraction_confidence",
    "extraction_attempts", "review_verdict", "review_route", "reviewer_doc_type",
    "reviewer_confidence", "classification_arbiter_decision", "judge_verdict", "judge_score",
    "arbiter_decision", "conflict_detected",
)


def build_handoff(state: Mapping[str, Any]) -> dict[str, Any]:
    """The small dict of a node's key outputs a backend gets (no document text).

    Scalars the nodes already write (confidences, attempts, verdicts), the guardrail
    flags, and, when there is an extraction, the extraction-gate handoff (class, sorter
    subclass, deep-copied public fields).
    """
    out: dict[str, Any] = {
        k: state[k] for k in _HANDOFF_SCALARS
        if isinstance(state.get(k), (str, int, float, bool))
    }
    guardrails = state.get("extraction_guardrail")
    if isinstance(guardrails, (list, tuple)) and guardrails:
        out["extraction_guardrail"] = [str(g) for g in guardrails]
    extracted = state.get("extracted_data")
    doc_type = state.get("doc_type")
    if isinstance(extracted, Mapping) and extracted and isinstance(doc_type, str):
        from agents.extraction_gate import extraction_gate_handoff

        out["extraction"] = extraction_gate_handoff(doc_type, extracted, _subclass(state))
    return out


def _subclass(state: Mapping[str, Any]) -> "str | None":
    return next(
        (v.strip() for v in (state.get("doc_subclass"), state.get("contract_subtype"))
         if isinstance(v, str) and v.strip()),
        None,
    )


def _build_input(policy: SlotPolicy, state: Mapping[str, Any]) -> DecisionInput:
    text = state.get("doc_text")
    doc_id = state.get("doc_id")
    doc_type = state.get("doc_type")
    return DecisionInput(
        slot=policy.slot,
        node=policy.node,
        doc_id=doc_id if isinstance(doc_id, str) else None,
        doc_type=doc_type if isinstance(doc_type, str) else None,
        doc_subclass=_subclass(state),
        doc_text=text if isinstance(text, str) else "",
        handoff=build_handoff(state),
        state=state,
    )


def _failed_outcome(policy: SlotPolicy, status: Status, reason: str, **extra: Any) -> DecisionOutcome:
    """The outcome of a consultation that gave no usable answer.

    ``fail_safe`` turns it into a park hint with a cause; ``fail_open`` leaves no hint.
    """
    evidence = {"reason": reason, **extra}
    if policy.on_failure == "fail_safe":
        return DecisionOutcome(
            route_hint=PARK, causes=(_FAILURE_CAUSE[status],), evidence=evidence, status=status
        )
    return DecisionOutcome(evidence=evidence, status=status)


def consult(policy: SlotPolicy, inp: DecisionInput) -> DecisionOutcome:
    """Ask ``policy``'s backend about ``inp``. Never raises (a ``KeyboardInterrupt`` aside).

    See the module docstring for the status of each failure. A usable answer is returned
    as the backend gave it (normalised), with any hint it carried; whether the hint may
    be followed is :func:`vet_outcome`'s call. Each consultation is its own
    ``consult-decision-point`` observation under the node's span, with slot, backend,
    node and status in its metadata (Langfuse tags are trace-level and immutable, so
    per-observation facts go in metadata) and no document text.
    """
    started = time.monotonic()
    with observation(
        "consult-decision-point",
        as_type="span",
        input={"slot": policy.slot, "node": policy.node, "doc_id": inp.doc_id, "doc_type": inp.doc_type},
        metadata={"slot": policy.slot, "backend": policy.backend, "node": policy.node,
                  "on_failure": policy.on_failure},
    ) as span:
        outcome = _consult(policy, inp)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        log = logger.info if outcome.status == "ok" else logger.warning
        if outcome.evidence.get("reason") == "backend_not_registered" and policy.on_failure == "fail_open":
            log = logger.debug  # the missing-backend warning above is already once per slot
        # The label is backend-supplied free text (it could quote the document): only its length is logged.
        log(
            "decision_point_consulted",
            slot=policy.slot, node=policy.node, backend=policy.backend, doc_id=inp.doc_id,
            status=outcome.status, route_hint=policy.loggable_hint(outcome.route_hint),
            label_length=len(outcome.label) if outcome.label is not None else None,
            confidence=outcome.confidence, causes=list(outcome.causes), latency_ms=latency_ms,
            reason=outcome.evidence.get("reason") if outcome.status != "ok" else None,
        )
        if span is not None:
            try:
                span.update(
                    output={
                        "status": outcome.status,
                        "route_hint": policy.loggable_hint(outcome.route_hint),
                        "label_length": len(outcome.label) if outcome.label is not None else None,
                        "confidence": outcome.confidence,
                        "causes": list(outcome.causes), "model": outcome.model,
                        "latency_ms": latency_ms,
                    },
                    metadata={"slot": policy.slot, "backend": policy.backend,
                              "node": policy.node, "status": outcome.status},
                )
            except Exception:  # tracing never fails a consultation
                logger.debug("decision_point_span_update_failed", slot=policy.slot)
        return outcome


def _consult(policy: SlotPolicy, inp: DecisionInput) -> DecisionOutcome:
    from llm.retry import is_transient_error
    from pipeline.limits import RunBudgetExceeded, RunDeadlineExceeded

    backend = get_decision_point_backend(policy.backend)
    if backend is None:
        # fail_open: the build-time warning already said so, so once per slot per process.
        # fail_safe parks every document for it, which stays loud.
        if policy.on_failure == "fail_safe" or _first_time(_warned_missing, policy.slot):
            logger.warning("decision_point_backend_missing", slot=policy.slot, backend=policy.backend,
                           on_failure=policy.on_failure)
        return _failed_outcome(policy, "unavailable", "backend_not_registered")
    try:
        # The answer is normalised inside the bounded call: reading a lazy mapping or an
        # iterator of causes is the backend's time too, and the bound returns a plain outcome.
        outcome, own_verdict = run_bounded(
            lambda: _normalize_answer(backend(inp)),
            name=f"decision-point-{policy.slot}",
            abandoned=_abandoned_threads,
            max_abandoned=_MAX_ABANDONED_DECISION_POINT_THREADS,
            lock=_abandoned_lock,
            timeout=policy.timeout_seconds,
        )
    except DecisionBackendTimeout:
        return _failed_outcome(policy, "unavailable", "timeout")
    except (RunDeadlineExceeded, RunBudgetExceeded) as exc:
        # The run's own limits: the next node boundary aborts the run; this slot just has no answer.
        return _failed_outcome(policy, "unavailable", "run_limit", error=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - a decision model never fails the run
        try:
            transient = is_transient_error(exc)
        except Exception:
            transient = False
        if transient:
            return _failed_outcome(policy, "unavailable", "transient_error", error=type(exc).__name__)
        return _failed_outcome(policy, "invalid", "backend_error", error=type(exc).__name__)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - SystemExit, GeneratorExit...: never leave the consultation
        return _failed_outcome(policy, "invalid", "backend_error", error=type(exc).__name__)
    if own_verdict:
        # Found malformed by normalisation: our own reason and detail tokens are kept.
        return _failed_outcome(
            policy, outcome.status, str(outcome.evidence["reason"]),
            **{k: v for k, v in outcome.evidence.items() if k in ("detail", "answer_type")},
        )
    if outcome.status != "ok":
        # Declared by the backend itself ("unavailable" / "invalid"): its hint is not trusted,
        # and on_failure decides. Nor is its evidence: a backend-written reason or detail is
        # free text that could quote the document, so only our own token is recorded.
        return _failed_outcome(policy, outcome.status, "backend_declined")
    return outcome


# ---------------------------------------------------------------------------
# Node wrappers
# ---------------------------------------------------------------------------


def _stored(outcome: DecisionOutcome, policy: SlotPolicy, doc_type: Any) -> dict[str, Any]:
    record = outcome.to_dict()
    record["backend"] = policy.backend
    # What the policy lets this outcome ask for. ``collect_review_causes`` reads it to tell
    # an outcome that parked the document from one that did not (it cannot see the config).
    record["vetted_route"] = vet_outcome(policy, record, doc_type)
    return record


_SPEC_BY_NODE = {spec.node: spec for spec in ROUTE_SPECS}


def _proposal(node: str, state: Mapping[str, Any]) -> "str | None":
    """The router's proposal for ``node`` on ``state``, or ``None`` when it cannot be asked.

    Asks the node's base router once. It is only called when a slot has a vetted hint, so
    the routers' own info logs repeat on that rare path only.
    """
    spec = _SPEC_BY_NODE.get(node)
    if spec is None:
        return None
    try:
        return spec.router(dict(state))  # type: ignore[arg-type]
    except Exception:
        return None


def _earlier_rule_answers(node: str, state: Mapping[str, Any], proposed: "str | None") -> bool:
    """Whether a rule evaluated before the ``decision_point:*`` rules answers for ``node``.

    :func:`~graph.route_spec.decide_route` returns the first valid destination a rule gives,
    so such a rule (declared on the spec, or registered ahead of the slots) takes the route
    and no slot of the node is applied or charged. Mirrors ``decide_route``'s validity check;
    a rule that raises or answers outside the node's destinations does not count.
    """
    from graph.route_spec import route_rules

    spec = _SPEC_BY_NODE.get(node)
    if spec is None:
        return False
    earlier = list(spec.rules)
    for rule in route_rules(node):
        if rule.name.startswith(RULE_PREFIX):
            break
        earlier.append(rule)
    view = MappingProxyType(dict(state))
    for rule in earlier:
        try:
            answer = rule.decide(view, proposed)
            if isinstance(answer, str) and type(answer) is not str:
                answer = str.__str__(answer)
            if isinstance(answer, str) and answer in spec.destinations:
                return True
        except Exception:  # noqa: BLE001 - decide_route skips a failing rule too
            continue
    return False


def _rule_answers(node: str, proposed: "str | None", hint: str) -> bool:
    """Whether the slot's rule would return ``hint`` against the router's ``proposed``.

    Mirrors :func:`rule_for`: nothing for a proposed park, self-loop or verification gate,
    nothing when the hint equals the proposal. An unknown proposal counts as an answer (the budget errs on
    the side of spending).
    """
    return proposed is None or (
        proposed != PARK and proposed != node and proposed not in GATE_PROPOSALS and proposed != hint
    )


def wrap_node(node: str, fn: Callable[[Any], Any], policies: Sequence[SlotPolicy]) -> Callable[[Any], Any]:
    """``fn`` with its slots consulted after it returns; ``fn`` itself when it has none.

    The node runs unchanged. If it returns a mapping, each of ``node``'s slots that is on
    for the document's class is consulted with the merged state (state plus the node's
    update), in config order, and the update gains ``decision_outcomes``: the previous
    outcomes with each of this node's slots rewritten or dropped. A node that asked for a
    transient retry (``transient_error``) is not consulted: its result is not usable, it
    will run again, and the router retries or parks it. The key is only written when an
    outcome was stored or removed, so a pass that consults nothing leaves state as it was.

    **Override budget.** A vetted hint to a destination other than ``human_review`` that
    the slot's rule will actually apply spends one of the slot's ``max_overrides`` for this
    document (``decision_override_counts``, never reset within a run). Rules run in config
    order and the first answer wins, so a slot is charged only when no earlier slot of the
    node already answers (an earlier slot that is capped, agrees with the router or has no
    hint does not shield it), and none is charged when a route rule evaluated before the
    slots (declared on the node's spec or registered ahead of them) already answers. Past the budget the outcome is stored with ``capped: true``
    and no vetted route, and the rule leaves the router's proposal alone, so hint cycles
    across slots end whatever the topology. Parks are exempt: they need a person, cannot
    loop on their own, and ``fail_safe`` must keep parking; an answering park still
    shields later slots.
    """
    mine = tuple(p for p in policies if p.node == node)
    if not mine:
        return fn

    @functools.wraps(fn)
    def wrapper(state: Any) -> Any:
        update = fn(state)
        if not isinstance(update, Mapping):
            return update
        merged = {**state, **update}
        previous = merged.get(OUTCOMES_KEY)
        outcomes = dict(previous) if isinstance(previous, Mapping) else {}
        prior_counts = merged.get(OVERRIDES_KEY)
        counts = dict(prior_counts) if isinstance(prior_counts, Mapping) else {}
        changed = counts_changed = claimed = False
        consulted: list = []  # (policy, stored), in config order
        for policy in mine:
            had = policy.slot in outcomes
            outcomes.pop(policy.slot, None)
            doc_type = merged.get("doc_type")
            if policy.resolve(doc_type).enabled and not merged.get("transient_error"):
                try:
                    inp = _build_input(policy, merged)
                except Exception as exc:  # noqa: BLE001  never raise into the graph
                    outcome = _failed_outcome(
                        policy, "invalid", "input_build_failed", error_type=type(exc).__name__
                    )
                else:
                    outcome = consult(policy, inp)
                stored = _stored(outcome, policy, doc_type)
                outcomes[policy.slot] = stored
                consulted.append((policy, stored))
                changed = True
            elif had:
                changed = True
        # Accounting runs on one snapshot: the state the router will see, with every slot of
        # this node already answered. A rule ahead of the slots is asked against that same
        # state, so whether it takes the route here is whether it takes it there.
        proposal: list = []  # asked once, only if some slot has a hint
        preempted: list = []
        for policy, stored in consulted:
            hint = stored["vetted_route"]
            if hint is None:
                continue
            if claimed:  # an earlier slot on this node already took the route
                stored["shadowed"] = True
                continue
            if not proposal:
                snapshot = {**merged, OUTCOMES_KEY: outcomes}
                proposal.append(_proposal(node, snapshot))
                preempted.append(_earlier_rule_answers(node, snapshot, proposal[0]))
            if preempted[0]:
                stored["shadowed"] = True  # an earlier, non-slot rule takes the route
                continue
            answers = _rule_answers(node, proposal[0], hint)
            if answers and hint != PARK:
                used = counts.get(policy.slot, 0)
                used = used if isinstance(used, int) and not isinstance(used, bool) else 0
                if used >= policy.max_overrides:
                    stored["capped"] = True
                    stored["vetted_route"] = None
                    answers = False
                else:
                    counts[policy.slot] = used + 1
                    counts_changed = True
            claimed = claimed or answers
        if not changed:
            return update
        out = {**update, OUTCOMES_KEY: outcomes}
        if counts_changed:
            out[OVERRIDES_KEY] = counts
        return out

    wrapper.mailroom_decision_slots = tuple(p.slot for p in mine)  # type: ignore[attr-defined]
    return wrapper


def wrap_human_review(fn: Callable[[Any], Any], policies: Sequence[SlotPolicy]) -> Callable[[Any], Any]:
    """``fn`` (the ``human_review`` node) clearing every outcome once a person has decided.

    A park is resolved by a person; whatever a model hinted before it concerned the earlier
    attempt, and must not route or label the next. Without slots, ``fn`` itself.
    """
    if not policies:
        return fn

    @functools.wraps(fn)
    def wrapper(state: Any) -> Any:
        update = fn(state)
        if not isinstance(update, Mapping) or not (
            isinstance(state, Mapping) and state.get(OUTCOMES_KEY) or update.get(OUTCOMES_KEY)
        ):
            return update
        return {**update, OUTCOMES_KEY: {}}

    wrapper.mailroom_clears_decision_outcomes = True  # type: ignore[attr-defined]
    return wrapper
