import yaml
from pathlib import Path
from functools import lru_cache

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


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


def get_all_doc_types() -> list[str]:
    cfg = load_config()
    return [cls["key"] for cls in cfg.get("doc_classes", [])]


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
