import contextlib
import contextvars
import math
import os
import re
import yaml
from pathlib import Path
from functools import lru_cache
from typing import Iterator, Literal

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a mapping with the same key twice.

    Plain YAML loaders keep the last occurrence silently, so a duplicated taxonomy key
    (for example a decision-point slot declared twice, or ``on_failure:`` written twice in
    one slot) would drop a setting without any error. Only the explicit keys of a mapping
    are checked: merge keys (``<<:``) are skipped, so an explicit key that overrides a
    merged one is not a duplicate. Used by :func:`load_config`, the runtime taxonomy reader.
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self._check_unique_keys(node)
        return super().construct_mapping(node, deep=deep)

    def _check_unique_keys(self, node) -> None:
        # Each mapping node is checked once, before SafeLoader flattens its merges into it
        # (flattening prepends the merged pairs, so a later look would see an explicit key
        # beside the one it overrides and call that a duplicate).
        checked = self.__dict__.setdefault("_unique_checked", set())
        if id(node) in checked:
            return
        checked.add(id(node))
        seen: set = set()
        merges = 0
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                merges += 1
                if merges > 1:  # ``<<`` written twice: SafeLoader would quietly combine both
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        "found duplicate key '<<'", key_node.start_mark,
                    )
                # The merge sources are mappings in their own right: an inline one is never
                # constructed on its own, so its keys are checked here. An explicit key of
                # ``node`` overriding a merged one stays legal.
                sources = value_node.value if isinstance(value_node, yaml.SequenceNode) else [value_node]
                for source in sources:
                    if isinstance(source, yaml.MappingNode):
                        self._check_unique_keys(source)
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable key: let SafeLoader report it
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            seen.add(key)


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.load(f, Loader=_UniqueKeyLoader)  # a SafeLoader subclass


def clear_config_cache() -> None:
    """Drop the cached taxonomy + the bins module-level copy (HUB-052).

    The mode toggle (``python -m pipeline.relations_mode`` / the API's
    ``/api/relations/mode``) edits taxonomy.yaml and clears the caches so the
    running process honors the flip immediately — no restart. Calls outside
    the process that made the edit (e.g. a standalone watcher) still need a
    restart; that's the CLI's ``--restart-watcher``.
    """
    load_config.cache_clear()
    try:
        import pipeline.bins as bins

        bins._config = None
    except Exception:
        pass


def get_agent_config(agent_name: str) -> dict:
    cfg = load_config()
    agents = cfg.get("agents", {})
    if agent_name not in agents:
        raise KeyError(f"Agent '{agent_name}' not found in taxonomy.yaml under agents:")
    return agents[agent_name]


def get_doc_class(doc_type: str) -> dict | None:
    cfg = load_config()
    for cls in cfg.get("doc_classes", []):
        if cls["key"] == doc_type:
            return cls
    return None


def get_confidence_thresholds(doc_type: str | None = None) -> dict:
    """Return confidence / Lane B budgets, optionally merged with per-class severity.

    Global keys always present. When ``doc_type`` resolves to a ``by_class``
    entry, that class's ``high`` / ``low`` / ``judge_band_high`` override the
    globals. Retry budgets (``retry_max``, ``arbiter_retry_max``,
    ``judge_max_passes``) stay global unless a class entry sets them.
    """
    cfg = load_config()
    base = dict(cfg.get("confidence", {}) or {})
    by_class = base.pop("by_class", None) or {}
    if doc_type:
        resolved = resolve_extract_class(doc_type) or doc_type
        overrides = by_class.get(resolved) if isinstance(by_class, dict) else None
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if value is not None:
                    base[key] = value
    return base


ORACLE_ROUTING_ENV = "MAILROOM_ORACLE_ROUTING"
_ORACLE_TRUE = frozenset({"1", "true", "yes", "on"})
_ORACLE_FALSE = frozenset({"0", "false", "no", "off"})
_ORACLE_OVERRIDE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "mailroom_oracle_routing_override", default=None
)


def oracle_routing_enabled() -> bool:
    """Whether routing may read ``ground_truth`` from the pipeline state (#207).

    Default **off**: a document is routed on what the pipeline observed, exactly as
    in production, even when a runner passes ``run_pipeline(ground_truth=...)`` for
    scoring and tracing. On, the six GT-aware routing arms (class miss in
    ``after_classify`` / ``after_retry_classify`` / ``after_classification_arbiter`` /
    ``after_review_classify``, expected-field coverage in ``after_extraction``) act
    on the label; such a run is oracle-assisted and its automation and accuracy
    numbers do not transfer to production.

    Resolution, first match wins: an enclosing :func:`oracle_routing_override`;
    ``MAILROOM_ORACLE_ROUTING`` (``1/true/yes/on`` or ``0/false/no/off``; empty or
    unrecognised values are ignored, so a typo cannot silently switch it on);
    taxonomy ``routing.use_ground_truth`` (only boolean ``true`` enables); off.
    Outside a run, the env var is read on every call. Pipeline execution freezes
    the resolved value in a run-scoped override before opening its trace, so routing
    agrees with that trace even if the process environment changes. The taxonomy
    is ``lru_cache``d like the rest of it.
    """
    forced = _ORACLE_OVERRIDE.get()
    if forced is not None:
        return forced
    raw = os.environ.get(ORACLE_ROUTING_ENV, "").strip().lower()
    if raw in _ORACLE_TRUE:
        return True
    if raw in _ORACLE_FALSE:
        return False
    block = load_config().get("routing")
    return isinstance(block, dict) and block.get("use_ground_truth") is True


@contextlib.contextmanager
def oracle_routing_override(enabled: bool) -> Iterator[None]:
    """Force the oracle-routing switch for the enclosed code (tests, offline replay).

    Wins over the env var and the taxonomy and is restored on exit; scoped to the
    current thread / task through a ``ContextVar``.
    """
    token = _ORACLE_OVERRIDE.set(bool(enabled))
    try:
        yield
    finally:
        _ORACLE_OVERRIDE.reset(token)


DEFAULT_MAX_CONCURRENT_DOCS = 4


def get_max_concurrent_docs() -> int:
    """Cap on pipeline runs the watcher executes at once.

    ``MAILROOM_MAX_CONCURRENT_DOCS`` overrides taxonomy.yaml
    ``watcher.max_concurrent_docs``. A missing, non-integer or non-positive
    value falls back to the next source and finally to the default (4): a bad
    value must never disable the cap or stall the watcher at zero workers.
    """
    import os

    candidates: list[object] = [os.environ.get("MAILROOM_MAX_CONCURRENT_DOCS")]
    block = load_config().get("watcher")
    if isinstance(block, dict):
        candidates.append(block.get("max_concurrent_docs"))
    for raw in candidates:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value >= 1:
            return value
    return DEFAULT_MAX_CONCURRENT_DOCS


def get_reviewer_config() -> dict:
    """Lane A reviewer mode block from taxonomy.yaml ``reviewer:``.

    ``default_mode`` applies to classes with no ``by_class`` entry. Entries
    may contain a ``mode`` and a decision confidence ``cutoff``. A missing
    or non-mapping reviewer block defaults to ``llm`` with no class entries;
    non-mapping class entries are ignored. Loading errors propagate.
    ``default_mode: decision_only`` selects the lane but cannot accept: a class
    without its own entry and cutoff always parks for human review.
    """
    block = load_config().get("reviewer")
    if not isinstance(block, dict):
        block = {}
    by_class = block.get("by_class")
    if not isinstance(by_class, dict):
        by_class = {}
    return {
        "default_mode": str(block.get("default_mode") or "llm"),
        "by_class": {
            str(key): dict(entry)
            for key, entry in by_class.items()
            if isinstance(entry, dict)
        },
    }


def get_review_mode(doc_type: str | None) -> Literal["llm", "decision_only"]:
    """Reviewer mode for a sorter label; only an explicit ``decision_only``
    selects the decision lane. A ``by_class`` entry without that mode is
    ``llm``."""
    cfg = get_reviewer_config()
    entry = cfg["by_class"].get(doc_type) if isinstance(doc_type, str) else None
    mode = entry.get("mode") if entry is not None else cfg["default_mode"]
    return "decision_only" if mode == "decision_only" else "llm"


class DecisionPointConfigError(ValueError):
    """taxonomy.yaml ``decision_points:`` is malformed (or disagrees with the graph)."""


DECISION_POINT_ON_FAILURE = ("fail_open", "fail_safe")
DECISION_POINT_MAX_TIMEOUT_SECONDS = 3600  # one hour; anything longer is a typo, not a decision call
_DP_SLOT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\Z")
_DP_SLOT_KEYS = frozenset({
    "node", "backend", "enabled", "timeout_seconds", "on_failure", "allow", "min_confidence", "by_class",
    "max_overrides",
})
_DP_CLASS_KEYS = frozenset({"enabled", "allow", "min_confidence"})


def _dp_is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dp_unit(value: object) -> bool:
    # Bounds first: math.isfinite raises OverflowError on an oversized int.
    return _dp_is_number(value) and 0.0 <= value <= 1.0 and math.isfinite(value)  # type: ignore[operator]


def _dp_allow(value: object, where: str, problems: list[str]) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{where}: allow must be a list of destination names")
        return None
    if len(set(value)) != len(value):
        problems.append(f"{where}: allow lists a destination twice")
    return list(dict.fromkeys(value))


def get_decision_points_config() -> dict[str, dict]:
    """Pluggable decision points from taxonomy.yaml ``decision_points:``, normalised.

    Returns ``{slot: {node, backend, enabled, timeout_seconds, on_failure, allow,
    min_confidence, max_overrides, by_class}}`` in file order. A missing, empty or ``null`` section
    is ``{}``: no decision point, no wrapper, no rule, the pipeline runs as before.
    Defaults: ``enabled`` true (declaring a slot turns it on), ``timeout_seconds``
    ``None`` (``run_limits.llm_call_timeout_seconds`` at call time; a set value is above 0
    and at most ``DECISION_POINT_MAX_TIMEOUT_SECONDS``, 3600), ``on_failure``
    ``fail_open``, ``allow`` empty (the outcome is recorded and never routes),
    ``min_confidence`` ``None``, ``max_overrides`` 1 (how often the slot may redirect a
    document to a non-park destination), ``by_class`` ``{}`` holding only the keys a
    class sets. Every structural problem is reported at once in one
    :class:`DecisionPointConfigError`. Whether a slot's node and destinations exist is
    checked against the graph by ``graph.decision_points.validate_slot_policies``.
    The taxonomy is ``lru_cache``d: edits need a watcher/API restart.
    """
    block = load_config().get("decision_points")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise DecisionPointConfigError("decision_points must be a mapping of slot name to slot settings")
    classes = get_sorter_label_set()
    problems: list[str] = []
    out: dict[str, dict] = {}
    for slot, raw in block.items():
        where = f"decision_points.{slot}"
        if not isinstance(slot, str) or not _DP_SLOT_NAME.match(slot):
            problems.append(f"{where}: slot names are lower-case words joined by dots, e.g. 'extract.post'")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        unknown = sorted(str(k) for k in raw if k not in _DP_SLOT_KEYS)
        if unknown:
            problems.append(f"{where}: unknown key(s) {unknown}; allowed: {sorted(_DP_SLOT_KEYS)}")
        entry: dict = {
            "node": None, "backend": None, "enabled": True, "timeout_seconds": None,
            "on_failure": "fail_open", "allow": [], "min_confidence": None, "by_class": {},
            "max_overrides": 1,
        }
        for key in ("node", "backend"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: {key} is required (a non-empty string)")
            else:
                entry[key] = value.strip()
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                problems.append(f"{where}: enabled must be true or false")
            else:
                entry["enabled"] = raw["enabled"]
        if raw.get("timeout_seconds") is not None:
            t = raw["timeout_seconds"]
            # Bounds first: math.isfinite raises OverflowError on an int too large for a float.
            if not _dp_is_number(t) or not 0 < t <= DECISION_POINT_MAX_TIMEOUT_SECONDS:  # type: ignore[operator]
                problems.append(
                    f"{where}: timeout_seconds must be a number above 0 and at most "
                    f"{DECISION_POINT_MAX_TIMEOUT_SECONDS}"
                )
            else:
                entry["timeout_seconds"] = float(t)  # type: ignore[arg-type]
        if "on_failure" in raw:
            if raw["on_failure"] not in DECISION_POINT_ON_FAILURE:
                problems.append(f"{where}: on_failure must be one of {list(DECISION_POINT_ON_FAILURE)}")
            else:
                entry["on_failure"] = raw["on_failure"]
        if raw.get("allow") is not None:
            allow = _dp_allow(raw["allow"], where, problems)
            if allow is not None:
                entry["allow"] = allow
        if raw.get("min_confidence") is not None:
            if not _dp_unit(raw["min_confidence"]):
                problems.append(f"{where}: min_confidence must be a number from 0 to 1")
            else:
                entry["min_confidence"] = float(raw["min_confidence"])
        if "max_overrides" in raw:
            m = raw["max_overrides"]
            if not isinstance(m, int) or isinstance(m, bool) or m < 0:
                problems.append(f"{where}: max_overrides must be an integer of 0 or more")
            else:
                entry["max_overrides"] = m
        by_class = raw.get("by_class")
        if by_class is not None and not isinstance(by_class, dict):
            problems.append(f"{where}: by_class must be a mapping of class to overrides")
        for cls, override in (by_class or {}).items() if isinstance(by_class, dict) else ():
            cwhere = f"{where}.by_class.{cls}"
            if cls not in classes:
                problems.append(f"{cwhere}: unknown class; known: {sorted(classes)}")
                continue
            if not isinstance(override, dict):
                problems.append(f"{cwhere}: must be a mapping")
                continue
            unknown = sorted(str(k) for k in override if k not in _DP_CLASS_KEYS)
            if unknown:
                problems.append(f"{cwhere}: unknown key(s) {unknown}; allowed: {sorted(_DP_CLASS_KEYS)}")
            item: dict = {}
            if "enabled" in override:
                if not isinstance(override["enabled"], bool):
                    problems.append(f"{cwhere}: enabled must be true or false")
                else:
                    item["enabled"] = override["enabled"]
            if override.get("allow") is not None:
                allow = _dp_allow(override["allow"], cwhere, problems)
                if allow is not None:
                    item["allow"] = allow
            if override.get("min_confidence") is not None:
                if not _dp_unit(override["min_confidence"]):
                    problems.append(f"{cwhere}: min_confidence must be a number from 0 to 1")
                else:
                    item["min_confidence"] = float(override["min_confidence"])
            entry["by_class"][cls] = item
        out[slot] = entry
    if problems:
        raise DecisionPointConfigError("invalid decision_points config:\n  - " + "\n  - ".join(problems))
    return out


def get_all_doc_types() -> list[str]:
    """Live (non-retired) document class keys from taxonomy.yaml.

    Retired entries (``status: retired``) are excluded — they are retained
    machinery for backward-compatible dispatch but not live sorter outputs.
    """
    cfg = load_config()
    return [
        cls["key"]
        for cls in cfg.get("doc_classes", [])
        if cls.get("status") != "retired"
    ]


# Routing token the sorter / Lane A reviewer may emit when no live class fits
# (retired types, court opinions, DD memos). Not a taxonomy class and not a
# specialist — after_classify / after_retry_classify park it for human review.
UNKNOWN_DOC_TYPE = "unknown"

# Sorter / HF labels that extract through a live taxonomy specialist without
# adding a new doc_class row. Retired classes (court_opinion, due_diligence)
# are deliberately absent — they still park. ``merger_agreement`` is a live
# MAUD class (not an alias of CUAD ``contract``).
EXTRACT_CLASS_ALIASES: dict[str, str] = {}


def resolve_extract_class(doc_type: str | None) -> str | None:
    """Map a sorter label to the live taxonomy class used for extraction.

    Live taxonomy keys pass through. Extract aliases resolve to their
    specialist class. Unknown / retired / empty return None — never extract.
    ``merger_agreement`` is a live taxonomy key (MAUD), not an alias of
    ``contract`` (CUAD).
    """
    if not doc_type:
        return None
    live = get_all_doc_types()
    if doc_type in live:
        return doc_type
    aliased = EXTRACT_CLASS_ALIASES.get(doc_type)
    if aliased and aliased in live:
        return aliased
    return None


def is_extractable_doc_type(doc_type: str | None) -> bool:
    """True when ``doc_type`` is a live taxonomy class or an extract alias."""
    return resolve_extract_class(doc_type) is not None


def get_sorter_label_set() -> set[str]:
    """Labels the sorter (and Lane A reviewer) may emit.

    Live taxonomy classes plus ``unknown`` plus any extract aliases.
    ``unknown`` is a routing token, not a specialist class — routers park
    it; extract never dispatches it. ``merger_agreement`` is a live
    taxonomy class (MAUD), not an extract alias of ``contract``.
    """
    return set(get_all_doc_types()) | {UNKNOWN_DOC_TYPE} | set(EXTRACT_CLASS_ALIASES)


def get_doc_class_catalog() -> list[dict[str, str]]:
    """Sorter prompt catalog from taxonomy.yaml (key / label / description)."""
    cfg = load_config()
    out: list[dict[str, str]] = []
    for dc in cfg.get("doc_classes", []) or []:
        key = dc.get("key")
        if not key:
            continue
        out.append(
            {
                "key": str(key),
                "label": str(dc.get("label") or str(key).replace("_", " ").title()),
                "description": (dc.get("description") or "").strip()
                or str(key).replace("_", " "),
            }
        )
    return out


def get_extraction_schema_name(doc_type: str) -> str | None:
    resolved = resolve_extract_class(doc_type) or doc_type
    cls = get_doc_class(resolved)
    if cls:
        return cls.get("schema")
    return None
