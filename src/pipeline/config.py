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


def get_confidence_thresholds() -> dict:
    cfg = load_config()
    return cfg.get("confidence", {})


def get_all_doc_types() -> list[str]:
    cfg = load_config()
    return [cls["key"] for cls in cfg.get("doc_classes", [])]


# Routing token the sorter / Lane A reviewer may emit when no live class fits
# (retired types, court opinions, DD memos). Not a taxonomy class and not a
# specialist — after_classify / after_retry_classify park it for human review.
UNKNOWN_DOC_TYPE = "unknown"

# Sorter / HF labels that extract through a live taxonomy specialist without
# adding a new doc_class row. ``merger_agreement`` is the docclass-merged /
# MAUD label; the contracts specialist owns the schema. Retired classes
# (court_opinion, due_diligence) are deliberately absent — they still park.
EXTRACT_CLASS_ALIASES: dict[str, str] = {
    "merger_agreement": "contract",
}


def resolve_extract_class(doc_type: str | None) -> str | None:
    """Map a sorter label to the live taxonomy class used for extraction.

    Live taxonomy keys pass through. Extract aliases (``merger_agreement`` →
    ``contract``) resolve to their specialist class. Unknown / retired /
    empty return None — never extract.
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

    Live taxonomy classes plus ``unknown`` plus extract aliases so structured
    output can emit ``merger_agreement``. ``unknown`` is a routing token, not
    a specialist class — routers park it; extract never dispatches it.
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
