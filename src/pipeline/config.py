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


def get_extraction_schema_name(doc_type: str) -> str | None:
    cls = get_doc_class(doc_type)
    if cls:
        return cls.get("schema")
    return None
