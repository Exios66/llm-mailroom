"""Decision-model slots: typed places where a decision model may change what happens to a document.

Taxonomy ``decision_slots:`` (``src/config/taxonomy.yaml``). Every slot ships ``mode:
baseline``, which is exactly the pipeline without this module: no node is wrapped, no route
rule is registered, no slot state key is written and no model is called (a registered
extraction gate's record carries the ``attempt`` it judged either way). The slots are the
places the decision-model experiments (Sets A-F) measured or plan to measure, plus two
approval checks:

``sorter_reviewer``
    The Lane A decision reviewer (``reviewer:`` block, ``decision_only`` classes) gains a
    ``park_cap``: the share of reviewed documents it may park. Past the cap a document is
    reviewed by the chat reviewer instead (the baseline), so a miscalibrated decision model
    cannot flood the review queue.
``archive_gate``
    ``mode: enforce`` lets the registered extraction gate (``agents.extraction_gate``) stop
    an archive. Its verdict is derived **in code** from the per-field ``field_support``
    probabilities ("is this value supported by the source"): any field below
    ``gate_support_min`` means the extraction is not supported. The model's own ``verdict``
    answer is recorded but never routes. A document the router would archive parks with the
    cause ``archive_gate_unsupported`` while the slot's ``park_cap`` allows. With
    ``release: true`` the gate may also send a document the router would park straight to
    the report, but only when every review cause is soft (low extraction confidence, spent
    retries, a ``partial`` judge verdict), the gate answered every key field the taxonomy
    names for the class (``gate_key_fields``; an unanswered key field is support 0) at
    ``release_support_min`` or above, and the ``release_cap`` allows. It never releases a document the
    judge called ``incomplete``, a schema, guardrail, chunk, vocabulary or coverage failure,
    or a matter conflict.
``judge_replacement``
    ``mode: gate``: ``judge_verify`` asks no LLM when the gate has answered for the current
    extraction attempt and the class has ``gate_key_fields`` (else the LLM judge runs); the verdict is ``complete`` when the gate supports every field, else
    ``incomplete`` with the unsupported fields as findings (``judge_source:
    extraction_gate``; no judge score is emitted and ``judge_coverage`` is cleared). The
    arbiter and its retry budget then work as before, with one rule: an
    ``accept_with_caveats`` on a gate ``incomplete``/``partial`` never archives (cause
    ``gate_incomplete``; never capped). With no usable gate answer the LLM judge runs (the
    baseline).
``arbiter_veto``
    ``mode: enforce``: an arbiter ``accept_with_caveats`` that the gate disagrees with parks
    (cause ``arbiter_veto_gate``). A veto only parks; it never releases. It does not spend
    its cap on a document ``judge_replacement`` already parks (``action: hold``).
``judge_check`` / ``boss_check``
    ``mode: enforce``: a judge ``complete`` (``judge_verify``) or a boss ``approved``
    (``boss_escalation``) about to archive must be backed by the gate: disagreement parks
    (``judge_check_unsupported``, ``boss_check_unsupported``), and with ``on_unverified:
    park`` so does an approval the gate cannot confirm (no usable answer, or a class without
    ``gate_key_fields``: ``judge_complete_unverified``, ``boss_approval_unverified``).
    ``arbiter_veto.on_unverified`` does the same for an ``accept_with_caveats``
    (``arbiter_accept_unverified``). They only park, within ``park_cap`` (10%). Taxonomy
    ``confidence`` already parks a judge ``complete`` and an ``accept_with_caveats`` by
    default (``graph/routing.py``); these checks matter where a class opts back in.
``closed_field_resolver``
    #285 parks any closed-label value off its taxonomy vocabulary. ``mode: suggest`` asks the
    registered resolver (``agents.closed_field_resolver``) to pick an allowed value and
    records the pick for the reviewer; ``mode: resolve`` also writes the pick into the
    extraction when every off-list field got an on-list pick at ``min_probability`` or above
    and the ``release_cap`` allows. The original values stay in
    ``extracted_data["_closed_field_resolutions"]`` and the state.
``field_review``
    ``mode: annotate`` (exploratory): a parked document's review block names the few fields a
    person should check (``review_focus``: gate-unsupported and off-list fields, with the
    resolver's pick when there is one), when there are at most ``max_fields`` of them.
    Routing is unchanged.

**Key fields.** ``gate_key_fields`` names, per class, the fields the gate must answer; it is
the mailroom's own list, so coverage never rests on what the plug-in says it asked. Field
names the gate returns that are not fields of the class's extraction schema are dropped
(at most 64 are read), so plug-in text never reaches findings or review evidence.

**Caps** are rates over the documents a slot has seen in this process: a park or release is
allowed while ``count + 1 <= cap * max(seen, cap_warmup)``. ``cap_warmup`` (default 20) keeps
the first documents from opening a cap wide (a 3% release cap allows no release before
document 34). Each document is counted once per slot. A capped decision follows the
baseline route and is recorded with ``action: capped``. The ledger is per process (each API
or watcher worker keeps its own); :func:`reset_ledger` starts a fresh one (the sandbox calls
it per run) and :func:`ledger_snapshot` reports it.

**Records.** ``state["decision_slot_outcomes"][slot]`` holds the latest decision of each
slot: ``node``, ``attempt``, ``proposal`` (the base router's destination), ``action``
(``park``, ``release``, ``capped``, ``agree``, ``hold``, ``unavailable``), ``route`` (the
destination the slot's route rule applies, else ``None``), ``cause`` and the gate numbers.
A route rule ``decision_slot:<node>`` applies ``route`` only while the router still proposes
what the slot saw. Once a person resolves a park, the outcomes are cleared (the marks stay),
so a decision about an earlier attempt never labels the next. ``state["decision_slot_marks"]`` lists ``seen:<slot>``, ``park:<slot>``
and ``release:<slot>`` so a document is counted once.
"""

from __future__ import annotations

import functools
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import structlog

logger = structlog.get_logger(__name__)

OUTCOMES_KEY = "decision_slot_outcomes"
MARKS_KEY = "decision_slot_marks"
RESOLUTIONS_KEY = "closed_field_resolutions"
RULE_PREFIX = "decision_slot:"
PARK = "human_review"
ARCHIVE = "compile_report"

SLOTS = (
    "sorter_reviewer",
    "archive_gate",
    "judge_replacement",
    "arbiter_veto",
    "closed_field_resolver",
    "field_review",
    "judge_check",
    "boss_check",
)
_MODES: dict[str, tuple[str, ...]] = {
    "sorter_reviewer": ("baseline",),
    "archive_gate": ("baseline", "enforce"),
    "judge_replacement": ("baseline", "gate"),
    "arbiter_veto": ("baseline", "enforce"),
    "closed_field_resolver": ("baseline", "suggest", "resolve"),
    "field_review": ("baseline", "annotate"),
    "judge_check": ("baseline", "enforce"),
    "boss_check": ("baseline", "enforce"),
}
_KEYS: dict[str, tuple[str, ...]] = {
    "sorter_reviewer": ("mode", "park_cap"),
    "archive_gate": ("mode", "park_cap", "release", "release_support_min", "release_cap"),
    "judge_replacement": ("mode",),
    "arbiter_veto": ("mode", "park_cap", "on_unverified"),
    "closed_field_resolver": ("mode", "min_probability", "release_cap"),
    "field_review": ("mode", "max_fields"),
    "judge_check": ("mode", "park_cap", "on_unverified"),
    "boss_check": ("mode", "park_cap", "on_unverified"),
}
# What an approval check does when the gate cannot confirm the approval (no usable answer, or
# the class has no gate_key_fields): follow the baseline, or park.
_ON_UNVERIFIED = ("baseline", "park")
_TOP_KEYS = ("gate_support_min", "cap_warmup", "gate_key_fields", *SLOTS)
_MAX_GATE_FIELDS = 64
# Review causes a gate release may clear: none of them is hard evidence of a bad extraction.
_SOFT_CAUSES = frozenset({"low_extraction_confidence", "retries_exhausted", "judge_partial",
                          "judge_complete_unconfirmed"})
_MAX_RESOLVED_FIELDS = 8


class DecisionSlotConfigError(ValueError):
    """taxonomy.yaml ``decision_slots:`` is malformed."""


@dataclass(frozen=True)
class SlotsConfig:
    gate_support_min: float = 0.5
    cap_warmup: int = 20
    reviewer_park_cap: float | None = None
    archive_gate: str = "baseline"
    archive_park_cap: float | None = 0.10
    archive_release: bool = False
    archive_release_support_min: float = 0.9
    archive_release_cap: float | None = 0.03
    judge_replacement: str = "baseline"
    arbiter_veto: str = "baseline"
    veto_park_cap: float | None = None
    veto_on_unverified: str = "baseline"
    resolver: str = "baseline"
    resolver_min_probability: float = 0.9
    resolver_release_cap: float | None = 0.03
    field_review: str = "baseline"
    field_review_max_fields: int = 3
    judge_check: str = "baseline"
    judge_check_park_cap: float | None = 0.10
    judge_check_on_unverified: str = "baseline"
    boss_check: str = "baseline"
    boss_check_park_cap: float | None = 0.10
    boss_check_on_unverified: str = "baseline"
    gate_key_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def nodes_to_wrap(self) -> frozenset[str]:
        """The graph nodes whose result a routing slot reads (empty when every slot is baseline)."""
        nodes: set[str] = set()
        if self.archive_gate == "enforce":
            nodes |= {"extract", "retry_extract", "judge_verify"}
        if self.arbiter_veto == "enforce" or self.judge_replacement == "gate":
            nodes.add("arbiter")
        if self.judge_check == "enforce":
            nodes.add("judge_verify")
        if self.boss_check == "enforce":
            nodes.add("boss_escalation")
        return frozenset(nodes)

    def any_on(self) -> bool:
        """True when some slot is not at its baseline."""
        return bool(self.nodes_to_wrap()) or self.reviewer_park_cap is not None or (
            self.resolver != "baseline") or self.field_review != "baseline"


def _unit(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    # An int is compared exactly: math.isfinite() overflows on one too large for a float.
    return (isinstance(value, int) or math.isfinite(value)) and 0 <= value <= 1


def _cap(where: str, value: Any) -> float | None:
    if value is None:
        return None
    if not _unit(value):
        raise DecisionSlotConfigError(f"decision_slots.{where} must be null or a number in [0, 1], got {value!r}")
    return float(value)


def _prob(where: str, value: Any) -> float:
    if not _unit(value):
        raise DecisionSlotConfigError(f"decision_slots.{where} must be a number in [0, 1], got {value!r}")
    return float(value)


def _on_unverified(slot: str, block: Mapping[str, Any]) -> str:
    value = block.get("on_unverified", "baseline")
    if isinstance(value, bool) or value not in _ON_UNVERIFIED:
        raise DecisionSlotConfigError(
            f"decision_slots.{slot}.on_unverified must be one of {list(_ON_UNVERIFIED)}, got {value!r}"
        )
    return value


def _key_fields(raw: Any) -> dict[str, tuple[str, ...]]:
    """``gate_key_fields``: ``{class: [field, ...]}``, every field a field of the class's schema."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DecisionSlotConfigError("decision_slots.gate_key_fields must map doc classes to field lists")
    out: dict[str, tuple[str, ...]] = {}
    for cls, names in raw.items():
        where = f"decision_slots.gate_key_fields.{cls}"
        if not isinstance(names, (list, tuple)) or not names or not all(isinstance(n, str) and n for n in names):
            raise DecisionSlotConfigError(f"{where} must be a non-empty list of field names")
        if len(set(names)) != len(names) or len(names) > _MAX_GATE_FIELDS:
            raise DecisionSlotConfigError(f"{where} must name distinct fields, at most {_MAX_GATE_FIELDS}")
        known = _schema_fields(str(cls))
        unknown = sorted(set(names) - known) if known is not None else []
        if unknown:
            raise DecisionSlotConfigError(f"{where}: {unknown} are not fields of the {cls} extraction schema")
        out[str(cls)] = tuple(names)
    return out


@functools.lru_cache(maxsize=32)
def _schema_field_set(doc_type: str) -> frozenset[str] | None:
    try:
        from schemas import EXTRACTION_SCHEMAS
    except ImportError:  # loaded by path (the sandbox preflight): field names are checked at startup
        return None
    model = EXTRACTION_SCHEMAS.get(doc_type)
    if model is None:
        raise DecisionSlotConfigError(f"decision_slots.gate_key_fields: {doc_type!r} has no extraction schema")
    return frozenset(model.model_fields)


def _schema_fields(doc_type: str) -> frozenset[str] | None:
    """The field names of ``doc_type``'s extraction schema; ``None`` when the schemas cannot be imported."""
    return _schema_field_set(doc_type)


def parse_slots(raw: Any) -> SlotsConfig:
    """Validate a ``decision_slots:`` mapping (``None`` or ``{}`` = every slot baseline)."""
    if raw is None:
        return SlotsConfig()
    if not isinstance(raw, Mapping):
        raise DecisionSlotConfigError("decision_slots must be a mapping")
    unknown = sorted(set(map(str, raw)) - set(_TOP_KEYS))
    if unknown:
        raise DecisionSlotConfigError(f"decision_slots: unknown keys {unknown} (known: {list(_TOP_KEYS)})")
    blocks: dict[str, Mapping[str, Any]] = {}
    for slot in SLOTS:
        block = raw.get(slot)
        if block is None:
            block = {}
        if not isinstance(block, Mapping):
            raise DecisionSlotConfigError(f"decision_slots.{slot} must be a mapping")
        bad = sorted(set(map(str, block)) - set(_KEYS[slot]))
        if bad:
            raise DecisionSlotConfigError(f"decision_slots.{slot}: unknown keys {bad} (known: {list(_KEYS[slot])})")
        mode = block.get("mode", "baseline")
        if isinstance(mode, bool):
            raise DecisionSlotConfigError(
                f"decision_slots.{slot}.mode is a YAML boolean ({mode!r}); write one of {list(_MODES[slot])}"
            )
        if mode not in _MODES[slot]:
            raise DecisionSlotConfigError(f"decision_slots.{slot}.mode must be one of {list(_MODES[slot])}, got {mode!r}")
        blocks[slot] = block
    warmup = raw.get("cap_warmup", 20)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 1:
        raise DecisionSlotConfigError(f"decision_slots.cap_warmup must be a positive integer, got {warmup!r}")
    release = blocks["archive_gate"].get("release", False)
    if not isinstance(release, bool):
        raise DecisionSlotConfigError(f"decision_slots.archive_gate.release must be true or false, got {release!r}")
    max_fields = blocks["field_review"].get("max_fields", 3)
    if isinstance(max_fields, bool) or not isinstance(max_fields, int) or max_fields < 1:
        raise DecisionSlotConfigError(f"decision_slots.field_review.max_fields must be a positive integer, got {max_fields!r}")
    ag, rv = blocks["archive_gate"], blocks["closed_field_resolver"]
    jc, bc = blocks["judge_check"], blocks["boss_check"]
    key_fields = _key_fields(raw.get("gate_key_fields"))
    return SlotsConfig(
        gate_support_min=_prob("gate_support_min", raw.get("gate_support_min", 0.5)),
        cap_warmup=warmup,
        reviewer_park_cap=_cap("sorter_reviewer.park_cap", blocks["sorter_reviewer"].get("park_cap")),
        archive_gate=ag.get("mode", "baseline"),
        archive_park_cap=_cap("archive_gate.park_cap", ag.get("park_cap", 0.10)),
        archive_release=release,
        archive_release_support_min=_prob("archive_gate.release_support_min", ag.get("release_support_min", 0.9)),
        archive_release_cap=_cap("archive_gate.release_cap", ag.get("release_cap", 0.03)),
        judge_replacement=blocks["judge_replacement"].get("mode", "baseline"),
        arbiter_veto=blocks["arbiter_veto"].get("mode", "baseline"),
        veto_park_cap=_cap("arbiter_veto.park_cap", blocks["arbiter_veto"].get("park_cap")),
        veto_on_unverified=_on_unverified("arbiter_veto", blocks["arbiter_veto"]),
        resolver=rv.get("mode", "baseline"),
        resolver_min_probability=_prob("closed_field_resolver.min_probability", rv.get("min_probability", 0.9)),
        resolver_release_cap=_cap("closed_field_resolver.release_cap", rv.get("release_cap", 0.03)),
        field_review=blocks["field_review"].get("mode", "baseline"),
        field_review_max_fields=max_fields,
        judge_check=jc.get("mode", "baseline"),
        judge_check_park_cap=_cap("judge_check.park_cap", jc.get("park_cap", 0.10)),
        judge_check_on_unverified=_on_unverified("judge_check", jc),
        boss_check=bc.get("mode", "baseline"),
        boss_check_park_cap=_cap("boss_check.park_cap", bc.get("park_cap", 0.10)),
        boss_check_on_unverified=_on_unverified("boss_check", bc),
        gate_key_fields=key_fields,
        raw=dict(raw),
    )


def get_slots() -> SlotsConfig:
    """The taxonomy's ``decision_slots:`` (cached with the taxonomy; edits need a restart)."""
    from pipeline.config import load_config

    return _parse_cached(load_config().get("decision_slots"))


_PARSE_LOCK = threading.Lock()
_parsed: tuple[Any, SlotsConfig] | None = None


def _parse_cached(raw: Any) -> SlotsConfig:
    """``parse_slots(raw)``, reused while the block is equal to the one parsed last."""
    global _parsed
    with _PARSE_LOCK:
        if _parsed is not None and _parsed[0] == raw:
            return _parsed[1]
    cfg = parse_slots(raw)
    import copy

    with _PARSE_LOCK:
        _parsed = (copy.deepcopy(raw), cfg)
    return cfg


# ---------------------------------------------------------------------------
# Rate caps (per process)
# ---------------------------------------------------------------------------

_LEDGER_LOCK = threading.Lock()
_ledger: dict[str, dict[str, int]] = {}


def reset_ledger() -> None:
    """Forget every slot's counts (a new run, a test)."""
    with _LEDGER_LOCK:
        _ledger.clear()


def ledger_snapshot() -> dict[str, dict[str, int]]:
    """``{slot: {"seen": n, "park": n, "release": n}}`` for this process."""
    with _LEDGER_LOCK:
        return {slot: dict(counts) for slot, counts in _ledger.items()}


def _mark_seen(slot: str, marks: list[str]) -> None:
    tag = f"seen:{slot}"
    if tag in marks:
        return
    marks.append(tag)
    with _LEDGER_LOCK:
        counts = _ledger.setdefault(slot, {"seen": 0, "park": 0, "release": 0})
        counts["seen"] += 1


def _charge(slot: str, kind: str, cap: float | None, warmup: int, marks: list[str]) -> bool:
    """Spend one ``kind`` (park / release) of ``slot`` for this document; False when the cap is spent."""
    tag = f"{kind}:{slot}"
    if tag in marks:  # this document was already counted
        return True
    with _LEDGER_LOCK:
        counts = _ledger.setdefault(slot, {"seen": 0, "park": 0, "release": 0})
        if cap is not None and counts[kind] + 1 > cap * max(counts["seen"], warmup) + 1e-9:
            return False
        counts[kind] += 1
    marks.append(tag)
    return True


# ---------------------------------------------------------------------------
# The gate answer, read in code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateView:
    available: bool
    reason: str | None = None
    accepts: bool = False
    unsupported: tuple[str, ...] = ()
    min_support: float | None = None
    field_support: Mapping[str, float] = field(default_factory=dict)
    # True when the taxonomy names the class's key fields (``gate_key_fields``): every one
    # counts, and a key field with no answer is support 0.0. False: only the answered fields are known.
    covered: bool = False

    def summary(self) -> dict[str, Any]:
        if not self.available:
            return {"gate": "unavailable", "gate_reason": self.reason}
        return {
            "gate": "supports" if self.accepts else "unsupported",
            "unsupported_fields": list(self.unsupported),
            "min_support": round(self.min_support, 4) if self.min_support is not None else None,
            "coverage": "key_fields" if self.covered else "answered_only",
        }


def _gate_problem(rec: Any, state: Mapping[str, Any]) -> str | None:
    """Why the gate record says nothing about the current attempt (``None`` = it does)."""
    if not isinstance(rec, Mapping):
        return "no_gate_answer"
    if rec.get("error"):
        return "gate_error"
    if rec.get("skipped"):
        return f"gate_skipped:{rec.get('skipped')}"
    attempt = rec.get("attempt")
    if attempt is None or attempt != state.get("extraction_attempts"):
        return "stale_answer"
    support = rec.get("field_support")
    if not isinstance(support, Mapping) or not support:
        return "no_field_support"
    return None


def _read_support(support: Mapping[Any, Any], known: frozenset[str] | None) -> dict[str, float] | None:
    """The schema-field answers (at most 64); ``None`` when one of them is malformed."""
    values: dict[str, float] = {}
    for name, p in support.items():
        if not isinstance(name, str) or (known is not None and name not in known):
            continue  # plug-in text that is not one of our field names is never stored or shown
        if not _unit(p):
            return None
        values[name] = float(p)
        if len(values) >= _MAX_GATE_FIELDS:
            break
    return values


def gate_view(state: Mapping[str, Any], support_min: float,
              key_fields: Mapping[str, tuple[str, ...]] | None = None) -> GateView:
    """The extraction gate's answer on the CURRENT attempt, judged from ``field_support`` only.

    Only field names of the class's extraction schema are read (at most 64); every
    ``key_fields[doc_type]`` field counts, unanswered as support 0.
    """
    rec = state.get("extraction_gate")
    problem = _gate_problem(rec, state)
    if problem is not None:
        return GateView(False, problem)
    doc_type = str(state.get("doc_type") or "")
    try:
        known = _schema_fields(doc_type)
    except DecisionSlotConfigError:
        known = None
    values = _read_support(rec["field_support"], known)
    if values is None:
        return GateView(False, "malformed_field_support")
    key = tuple((key_fields or {}).get(doc_type, ()))
    for name in key:  # a key field the gate did not answer can never count as supported
        values.setdefault(name, 0.0)
    if not values:
        return GateView(False, "no_field_support")
    unsupported = tuple(sorted(f for f, p in values.items() if p < support_min))
    return GateView(True, None, not unsupported, unsupported, min(values.values()), values, bool(key))


# ---------------------------------------------------------------------------
# Routing slots: node wrappers and their route rules
# ---------------------------------------------------------------------------


def _proposal(node: str, state: Mapping[str, Any]) -> str | None:
    from graph.route_spec import ROUTE_SPECS

    for spec in ROUTE_SPECS:
        if spec.node == node:
            try:
                return spec.router(dict(state))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - no proposal, no slot decision
                return None
    return None


def _release_blocker(state: Mapping[str, Any]) -> str | None:
    """Why the gate may not release this parking document (``None`` = it may)."""
    from pipeline.config import class_auto_archives
    from pipeline.reconsideration import collect_review_causes

    # A class with ``auto_archive: false`` always needs a person. Never spend the
    # release budget rewriting its route to ``compile_report`` — ``after_report``
    # would park it again and the 3% release cap would be wasted on a no-op.
    if not class_auto_archives(state.get("doc_type") if isinstance(state.get("doc_type"), str) else None):
        return "class_requires_review"
    if state.get("judge_verdict") in ("incomplete", "judge_error"):
        return f"judge_{state.get('judge_verdict')}"
    if state.get("conflict_detected"):
        return "conflict"
    if state.get("extraction_guardrail"):
        return "guardrail"
    try:
        causes = collect_review_causes(dict(state))
    except Exception:  # noqa: BLE001 - unknown causes: hold
        return "causes_unknown"
    if not causes:
        return "no_cause"
    hard = sorted(set(causes) - _SOFT_CAUSES)
    if hard:
        return "hard_cause:" + ",".join(hard)
    return None


def _archive_gate(cfg: SlotsConfig, state: Mapping[str, Any], proposal: str | None, marks: list[str]) -> dict | None:
    if proposal not in (ARCHIVE, PARK):
        return None
    if proposal == PARK and not cfg.archive_release:
        return None
    _mark_seen("archive_gate", marks)
    view = gate_view(state, cfg.gate_support_min, cfg.gate_key_fields)
    rec: dict[str, Any] = {"action": "unavailable", "route": None, **view.summary()}
    if not view.available:
        return rec
    if proposal == ARCHIVE:
        if view.accepts:
            return {**rec, "action": "agree"}
        if _charge("archive_gate", "park", cfg.archive_park_cap, cfg.cap_warmup, marks):
            return {**rec, "action": "park", "route": PARK, "cause": "archive_gate_unsupported"}
        return {**rec, "action": "capped", "cause": "archive_gate_unsupported"}
    blocker = _release_blocker(state)
    if blocker is None and not view.covered:
        blocker = "coverage_unknown"  # no key fields for the class: a partial answer must never release
    if blocker is None and (view.min_support or 0.0) < cfg.archive_release_support_min:
        blocker = "support_below_release_min"
    if blocker is not None:
        return {**rec, "action": "hold", "hold_reason": blocker}
    if _charge("archive_gate", "release", cfg.archive_release_cap, cfg.cap_warmup, marks):
        return {**rec, "action": "release", "route": ARCHIVE}
    return {**rec, "action": "capped"}


def _arbiter_slots(cfg: SlotsConfig, state: Mapping[str, Any], proposal: str | None, marks: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if proposal != ARCHIVE or state.get("arbiter_decision") != "accept_with_caveats":
        return out
    if (
        cfg.judge_replacement == "gate"
        and state.get("judge_source") == "extraction_gate"
        and state.get("judge_verdict") in ("incomplete", "partial")
    ):
        prior = (state.get(OUTCOMES_KEY) or {}).get("judge_replacement")
        replaced = {"replaced_verdict": prior.get("verdict")} if isinstance(prior, Mapping) else {}
        # Never capped: a gate ``incomplete`` is never auto-accepted.
        out["judge_replacement"] = {"action": "park", "route": PARK, "cause": "gate_incomplete", **replaced}
    if cfg.arbiter_veto == "enforce":
        held = (out.get("judge_replacement") or {}).get("route") == PARK
        out["arbiter_veto"] = _approval_check(
            "arbiter_veto", cfg, state, marks,
            cap=cfg.veto_park_cap, on_unverified=cfg.veto_on_unverified,
            causes=("arbiter_veto_gate", "arbiter_accept_unverified"),
            held_by="parked_by_judge_replacement" if held else None,
        )
    return out


def _approval_check(slot: str, cfg: SlotsConfig, state: Mapping[str, Any], marks: list[str], *,
                    cap: float | None, on_unverified: str, causes: tuple[str, str],
                    held_by: str | None = None) -> dict[str, Any]:
    """Check an approval (judge ``complete``, arbiter ``accept_with_caveats``, boss ``approved``) against the gate.

    The gate disagrees (a field below ``gate_support_min``): park with ``causes[0]``. The gate
    cannot confirm it (no usable answer, ``unavailable``; or an answer for a class without
    ``gate_key_fields``, ``unverified``): the baseline, or with ``on_unverified: park`` park
    with ``causes[1]``. Every park spends the slot's ``park_cap``; ``held_by`` names another
    slot that already parks this pass, so nothing is charged twice.
    """
    _mark_seen(slot, marks)
    view = gate_view(state, cfg.gate_support_min, cfg.gate_key_fields)
    rec: dict[str, Any] = {"action": "unavailable", "route": None, **view.summary()}
    if view.available and not view.accepts:
        cause = causes[0]
    elif view.available and view.covered:
        return {**rec, "action": "agree"}
    else:
        if view.available:
            rec.update(action="unverified", gate_reason="coverage_unknown")
        if on_unverified != "park":
            return rec
        cause = causes[1]
    if held_by is not None:
        return {**rec, "action": "hold", "hold_reason": held_by}
    if _charge(slot, "park", cap, cfg.cap_warmup, marks):
        return {**rec, "action": "park", "route": PARK, "cause": cause}
    return {**rec, "action": "capped", "cause": cause}


def _judge_check(cfg: SlotsConfig, state: Mapping[str, Any], proposal: str | None, marks: list[str],
                 decided: Mapping[str, dict]) -> dict | None:
    """``judge_check: enforce``: a judge ``complete`` about to archive must be backed by the gate."""
    if proposal != ARCHIVE or state.get("judge_verdict") != "complete":
        return None
    if cfg.judge_replacement == "gate" and state.get("judge_source") == "extraction_gate":
        return None  # the gate gave this verdict itself
    held = (decided.get("archive_gate") or {}).get("route") == PARK
    return _approval_check(
        "judge_check", cfg, state, marks,
        cap=cfg.judge_check_park_cap, on_unverified=cfg.judge_check_on_unverified,
        causes=("judge_check_unsupported", "judge_complete_unverified"),
        held_by="parked_by_archive_gate" if held else None,
    )


def _boss_check(cfg: SlotsConfig, state: Mapping[str, Any], proposal: str | None, marks: list[str]) -> dict | None:
    """``boss_check: enforce``: a boss ``approved`` about to archive must be backed by the gate."""
    if proposal != ARCHIVE or state.get("review_decision") != "approved":
        return None
    return _approval_check(
        "boss_check", cfg, state, marks,
        cap=cfg.boss_check_park_cap, on_unverified=cfg.boss_check_on_unverified,
        causes=("boss_check_unsupported", "boss_approval_unverified"),
    )


def _carried_outcomes(node: str, state: Any, update: Mapping[str, Any]) -> dict[str, Any]:
    """The outcomes after a pass of ``node``: the prior ones minus ``node``'s, plus what it changed."""
    prior = state.get(OUTCOMES_KEY) if isinstance(state, Mapping) else None
    # This node's earlier decisions never survive a new pass of it.
    outcomes = {
        k: v for k, v in (prior.items() if isinstance(prior, Mapping) else ())
        if not (isinstance(v, Mapping) and v.get("node") == node)
    }
    own = update.get(OUTCOMES_KEY)
    if isinstance(own, Mapping):
        # The node may hand back a copy of the prior outcomes (judge_verify does); only an
        # entry it changed is fresh, so a stale entry of this node never comes back.
        old = prior if isinstance(prior, Mapping) else {}
        outcomes.update({k: v for k, v in own.items() if k not in old or v != old[k]})
    return outcomes


def _decide(node: str, cfg: SlotsConfig, merged: Mapping[str, Any], proposal: str | None,
            marks: list) -> dict[str, dict]:
    """Each slot of ``node`` that is on, in order (a later slot sees the earlier decisions)."""
    decided: dict[str, dict] = {}
    checks = (
        ("archive_gate", node in ("extract", "retry_extract", "judge_verify") and cfg.archive_gate == "enforce",
         lambda: _archive_gate(cfg, merged, proposal, marks)),
        ("judge_check", node == "judge_verify" and cfg.judge_check == "enforce",
         lambda: _judge_check(cfg, merged, proposal, marks, decided)),
        ("boss_check", node == "boss_escalation" and cfg.boss_check == "enforce",
         lambda: _boss_check(cfg, merged, proposal, marks)),
    )
    for slot, on, run in checks:
        rec = run() if on else None
        if rec is not None:
            decided[slot] = rec
    if node == "arbiter":
        decided.update(_arbiter_slots(cfg, merged, proposal, marks))
    return decided


def wrap_node(node: str, fn: Callable[[Any], Any], cfg: SlotsConfig | None = None) -> Callable[[Any], Any]:
    """``fn`` with the routing slots of ``node`` decided after it returns; ``fn`` itself when none is on."""
    cfg = cfg or get_slots()
    if node not in cfg.nodes_to_wrap():
        return fn

    @functools.wraps(fn)
    def wrapper(state: Any) -> Any:
        update = fn(state)
        if not isinstance(update, Mapping):
            return update
        outcomes = _carried_outcomes(node, state, update)
        if update.get("transient_error"):
            return {**update, OUTCOMES_KEY: outcomes}
        merged = {**state, **update}
        marks = list(merged.get(MARKS_KEY) or [])
        proposal = _proposal(node, merged)
        attempt = merged.get("extraction_attempts")
        for slot, rec in _decide(node, cfg, merged, proposal, marks).items():
            outcomes[slot] = {**rec, "node": node, "attempt": attempt, "proposal": proposal}
            if rec.get("action") in ("park", "release", "capped"):
                logger.info("decision_slot_decided", slot=slot, node=node, doc_id=merged.get("doc_id"),
                            proposal=proposal, action=rec["action"], route=rec.get("route"))
        return {**update, OUTCOMES_KEY: outcomes, MARKS_KEY: marks}

    wrapper.mailroom_decision_slot_node = node  # type: ignore[attr-defined]
    return wrapper


def wrap_human_review(fn: Callable[[Any], Any], cfg: SlotsConfig | None = None) -> Callable[[Any], Any]:
    """``fn`` (the ``human_review`` node) clearing ``decision_slot_outcomes`` once a person decided.

    The marks stay (each document is still counted once per slot). Every slot baseline: ``fn``.
    """
    cfg = cfg or get_slots()
    if not cfg.any_on():
        return fn

    @functools.wraps(fn)
    def wrapper(state: Any) -> Any:
        update = fn(state)
        if not isinstance(update, Mapping) or not (isinstance(state, Mapping) and state.get(OUTCOMES_KEY)):
            return update
        return {**update, OUTCOMES_KEY: {}}

    wrapper.mailroom_clears_decision_slot_outcomes = True  # type: ignore[attr-defined]
    return wrapper


def _rule(node: str):
    from graph.route_spec import RouteRule

    def decide(state: Mapping[str, Any], proposed: str | None) -> str | None:
        outcomes = state.get(OUTCOMES_KEY)
        if not isinstance(outcomes, Mapping):
            return None
        parks = releases = False
        for rec in outcomes.values():
            if not isinstance(rec, Mapping) or rec.get("node") != node or rec.get("proposal") != proposed:
                continue
            parks = parks or rec.get("route") == PARK
            releases = releases or rec.get("route") == ARCHIVE
        if parks:  # a park wins over any release on the same pass
            return PARK
        return ARCHIVE if releases else None

    return RouteRule(f"{RULE_PREFIX}{node}", decide)


def clear_rules() -> None:
    """Remove every ``decision_slot:*`` route rule (idempotent)."""
    from graph.route_spec import replace_route_rules

    replace_route_rules(RULE_PREFIX, ())


def install_rules(cfg: SlotsConfig | None = None) -> None:
    """Make the ``decision_slot:*`` route rules exactly those ``cfg`` needs (none for baseline)."""
    from graph.route_spec import replace_route_rules

    cfg = cfg or get_slots()
    replace_route_rules(RULE_PREFIX, [(node, _rule(node)) for node in sorted(cfg.nodes_to_wrap())])


# ---------------------------------------------------------------------------
# Judge replacement
# ---------------------------------------------------------------------------


def gate_judge_update(state: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(verdict update, extra)`` for ``judge_verify`` under ``judge_replacement``.

    The verdict update is the judge verdict derived from the gate, or ``None`` when the LLM
    judge must run (slot baseline, or no usable gate answer for this attempt). ``extra`` is
    what the node adds to the LLM judge's update in that case (the fallback record).
    """
    cfg = get_slots()
    if cfg.judge_replacement != "gate":
        return None, {}
    view = gate_view(state, cfg.gate_support_min, cfg.gate_key_fields)
    outcomes = dict(state.get(OUTCOMES_KEY) or {})
    base = {"node": "judge_verify", "attempt": state.get("extraction_attempts"), "proposal": None, "route": None}
    if view.available and not view.covered:
        view = GateView(False, "coverage_unknown")  # no key fields for the class: the LLM judge decides
    if not view.available:
        logger.info("judge_replacement_fallback", doc_id=state.get("doc_id"), reason=view.reason)
        outcomes["judge_replacement"] = {**base, "action": "unavailable", **view.summary()}
        return None, {OUTCOMES_KEY: outcomes, "judge_source": "llm"}
    from graph import attempts as attempt_history

    label = "complete" if view.accepts else "incomplete"
    findings = [f"{name}: not supported by the source (p={view.field_support[name]:.2f})" for name in view.unsupported]
    # The gate's support numbers stay in the outcome; they are not a judge score, so the
    # arbiter and the audit trail never read them as one.
    score = None
    update: dict[str, Any] = {
        "judge_verdict": label,
        "judge_score": score,
        "judge_findings": findings,
        "judge_source_verified": False,
        "judge_source": "extraction_gate",
        "judge_coverage": None,
        "judge_pass_count": int(state.get("judge_pass_count") or 0) + 1,
        "transient_error": False,
        OUTCOMES_KEY: {**outcomes, "judge_replacement": {**base, "action": "replaced", "verdict": label,
                                                          "judge_pass": int(state.get("judge_pass_count") or 0) + 1,
                                                          **view.summary()}},
    }
    history = attempt_history.record_verdict(state, verdict=label, score=score, findings=findings, source_verified=False)
    if history is not None:
        update[attempt_history.HISTORY_KEY] = history
    logger.info("judge_replaced_by_gate", doc_id=state.get("doc_id"), verdict=label,
                unsupported=list(view.unsupported))
    return update, {}


# ---------------------------------------------------------------------------
# Sorter reviewer park cap
# ---------------------------------------------------------------------------


def cap_reviewer_park(state: Mapping[str, Any], update: dict[str, Any],
                      fallback: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """The decision reviewer's ``update``, or the chat reviewer's when its park cap is spent."""
    cfg = get_slots()
    if cfg.reviewer_park_cap is None or update.get("transient_error"):
        return update
    marks = list(state.get(MARKS_KEY) or [])
    _mark_seen("sorter_reviewer", marks)
    outcomes = dict(state.get(OUTCOMES_KEY) or {})
    base = {"node": "review_classify", "attempt": None, "proposal": None, "route": None}
    if update.get("review_route") == "extract":
        outcomes["sorter_reviewer"] = {**base, "action": "agree"}
        return {**update, OUTCOMES_KEY: outcomes, MARKS_KEY: marks}
    from pipeline.reconsideration import REVIEWER_UNAVAILABLE

    if REVIEWER_UNAVAILABLE in (update.get("reviewer_causes") or []):
        # A backend outage is not the model's park: it never spends the cap.
        outcomes["sorter_reviewer"] = {**base, "action": "unavailable"}
        return {**update, OUTCOMES_KEY: outcomes, MARKS_KEY: marks}
    if _charge("sorter_reviewer", "park", cfg.reviewer_park_cap, cfg.cap_warmup, marks):
        outcomes["sorter_reviewer"] = {**base, "action": "park"}
        return {**update, OUTCOMES_KEY: outcomes, MARKS_KEY: marks}
    logger.info("sorter_reviewer_park_capped", doc_id=state.get("doc_id"))
    llm = fallback(state)
    outcomes["sorter_reviewer"] = {**base, "action": "capped", "fallback": "chat_reviewer",
                                   "decision_causes": list(update.get("reviewer_causes") or [])}
    # The chat reviewer's verdict routes: clear the decision lane's route and causes.
    return {"review_route": None, "reviewer_causes": [], **llm, OUTCOMES_KEY: outcomes, MARKS_KEY: marks}


# ---------------------------------------------------------------------------
# Closed-field resolver
# ---------------------------------------------------------------------------

_abandoned_resolver_threads: list[threading.Thread] = []
_resolver_lock = threading.Lock()


def resolve_closed_fields(state: Mapping[str, Any], doc_type: str, result: Any,
                          attempt: int | None) -> tuple[Any, dict[str, Any]]:
    """``(result, state updates)``: off-list closed labels resolved or suggested by the resolver."""
    cfg = get_slots()
    if cfg.resolver == "baseline" or not isinstance(result, dict) or not result or result.get("_parse_error"):
        return result, {}
    from agents.closed_field_resolver import closed_field_handoff, get_closed_field_resolver
    from graph.decision_points import DecisionBackendTimeout, run_bounded
    from pipeline.label_vocab import canonical_label_value, get_label_vocabularies, out_of_vocabulary_fields

    marks = list(state.get(MARKS_KEY) or [])
    _mark_seen("closed_field_resolver", marks)
    updates: dict[str, Any] = {MARKS_KEY: marks}
    off = out_of_vocabulary_fields(doc_type, result)
    if not off:
        return result, {**updates, RESOLUTIONS_KEY: []}
    resolver = get_closed_field_resolver()
    if resolver is None:
        logger.warning("closed_field_resolver_unregistered", doc_id=state.get("doc_id"))
        return result, {**updates, RESOLUTIONS_KEY: [{"field": f, "original": _jsonable(v), "status": "no_resolver"}
                                                     for f, v in sorted(off.items())]}
    vocab = get_label_vocabularies(doc_type)
    subclass = next((v.strip() for v in (state.get("doc_subclass"), state.get("contract_subtype"))
                     if isinstance(v, str) and v.strip()), None)
    doc_text = str(state.get("doc_text") or "")
    records: list[dict[str, Any]] = []
    for name in sorted(off)[:_MAX_RESOLVED_FIELDS]:
        rec: dict[str, Any] = {"field": name, "original": _jsonable(off[name]), "attempt": attempt,
                               "suggested": None, "probability": None, "applied": False}
        handoff = closed_field_handoff(doc_type, name, off[name], vocab.get(name, ()), subclass)
        try:
            answer = run_bounded(lambda h=handoff: resolver(doc_text, h), name="closed-field-resolver",
                                 abandoned=_abandoned_resolver_threads, max_abandoned=4, lock=_resolver_lock)
        except DecisionBackendTimeout as exc:
            records.append({**rec, "status": "timeout"})
            continue
        except Exception as exc:  # noqa: BLE001 - advisory call: fail to the baseline park
            # Only our own tokens in stored evidence: the class name, never the backend's text.
            records.append({**rec, "status": "error", "error": type(exc).__name__})
            continue
        if not isinstance(answer, Mapping):
            records.append({**rec, "status": "malformed"})
            continue
        token = canonical_label_value(tuple(vocab.get(name, ())), answer.get("value")) \
            if isinstance(answer.get("value"), str) else None
        prob = answer.get("probability")
        rec.update(suggested=token, probability=float(prob) if _unit(prob) else None)
        rec["status"] = "off_list_answer" if token is None else (
            "confident" if rec["probability"] is not None and rec["probability"] >= cfg.resolver_min_probability
            else "unsure")
        records.append(rec)
    resolvable = len(records) == len(off) and all(r["status"] == "confident" for r in records)
    if cfg.resolver == "resolve" and resolvable:
        if _charge("closed_field_resolver", "release", cfg.resolver_release_cap, cfg.cap_warmup, marks):
            result = dict(result)
            for r in records:
                result[r["field"]] = r["suggested"]
                r["applied"] = True
            result["_closed_field_resolutions"] = [dict(r) for r in records]
        else:
            for r in records:
                r["status"] = "capped"
    logger.info("closed_fields_resolved", doc_id=state.get("doc_id"), mode=cfg.resolver,
                fields=[r["field"] for r in records], applied=any(r["applied"] for r in records))
    return result, {**updates, RESOLUTIONS_KEY: records}


def _jsonable(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)[:200]


# ---------------------------------------------------------------------------
# Field-level review focus
# ---------------------------------------------------------------------------


def review_focus(state: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The fields a person should check on a parked document, or ``None`` (check the whole document)."""
    cfg = get_slots()
    if cfg.field_review != "annotate":
        return None
    from pipeline.label_vocab import out_of_vocabulary_fields

    focus: dict[str, dict[str, Any]] = {}
    view = gate_view(state, cfg.gate_support_min, cfg.gate_key_fields)
    if view.available:
        for name in view.unsupported:
            focus[name] = {"field": name, "reason": "unsupported", "support": round(view.field_support[name], 4)}
    extracted = state.get("extracted_data")
    off = out_of_vocabulary_fields(state.get("doc_type"), extracted) if isinstance(extracted, dict) else {}
    picks = {r.get("field"): r for r in state.get(RESOLUTIONS_KEY) or [] if isinstance(r, Mapping)}
    for name in off:
        entry = focus.setdefault(name, {"field": name})
        entry["reason"] = "out_of_vocabulary" if "reason" not in entry else entry["reason"] + "+out_of_vocabulary"
        pick = picks.get(name)
        if pick and pick.get("suggested"):
            entry["suggested"] = pick["suggested"]
            entry["suggested_probability"] = pick.get("probability")
    if not focus or len(focus) > cfg.field_review_max_fields:
        return None
    return [focus[k] for k in sorted(focus)]


__all__ = [
    "DecisionSlotConfigError",
    "GateView",
    "SLOTS",
    "SlotsConfig",
    "cap_reviewer_park",
    "gate_judge_update",
    "gate_view",
    "get_slots",
    "install_rules",
    "ledger_snapshot",
    "parse_slots",
    "reset_ledger",
    "resolve_closed_fields",
    "review_focus",
    "wrap_node",
]
