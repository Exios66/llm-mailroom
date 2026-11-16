"""The label and intake contract ``mailroom-ml`` vendors.

Every value is read from the module that owns it (``schemas.doc_classes``,
``schemas.corpus``, ``config``, ``pipeline.intake``); nothing is restated here.
The golden cases are computed by the Dojo's own functions, so a consumer that
reproduces them byte for byte normalizes exactly as the Dojo scores.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import CONTRACT_SUBTYPES, MAUD_CONSIDERATION_TYPES, SUBTYPE_ALIASES, SUBTYPE_UNKNOWN
from ..pipeline.intake import INTAKE_PREP_STEPS, deterministic_normalize
from ..schemas.corpus import CORPUS_SUBCLASS_SURFACES, DOC_TYPE_SUBCLASSES, normalize_corpus_subclass
from ..schemas.doc_classes import LIVE_DOC_TYPES

__all__ = ["CONTRACT_SCHEMA", "INTAKE_CASES", "contract_path", "load_contract", "ml_contract"]

#: Bumped only when the document's shape changes, not when a table's values do.
CONTRACT_SCHEMA = "dojo.ml_contract/1"

#: Synthetic inputs for the intake clerk, one or more per prep step.
INTAKE_CASES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("already_clean", "Notice of claim.\nPolicy 42."),
    ("nfc_normalize", "Cafe\u0301 lease"),
    ("newline_unify", "Line one\r\nLine two\rLine three"),
    ("nbsp_to_space", "Total\u00a0due:\u00a0$1,200"),
    ("strip_zero_width", "Agree\u200bment\ufeff signed"),
    ("control_chars", "Para\x07graph\x0bend"),
    ("hyphen_unwrap", "The indemni-\nfication clause"),
    ("collapse_blank_runs", "Header\n\n\n\n\nBody"),
    ("collapse_horizontal_space", "Name:      Jane   Roe"),
    ("table_row_kept", "| Item |   Qty |\n| a |  1 |"),
    ("trim_edges", "\n\n   Dear Sir,   \n\n"),
)


def _subclass_table(doc_type: str) -> dict[str, str]:
    """Every corpus subclass surface of ``doc_type`` mapped to its canonical key."""
    surfaces = CORPUS_SUBCLASS_SURFACES.get(doc_type, ())
    extra = ("", "unknown_surface")
    return {surface: normalize_corpus_subclass(doc_type, surface) for surface in (*surfaces, *extra)}


def _parts() -> dict[str, Any]:
    """The contract's content, part by part, before digests are added."""
    live = [doc_type for doc_type in LIVE_DOC_TYPES]
    return {
        "doc_types": live,
        "subclasses": {doc_type: list(DOC_TYPE_SUBCLASSES.get(doc_type, ())) for doc_type in live},
        "subclass_unknown": SUBTYPE_UNKNOWN,
        "contract_subtypes": [{"key": item["key"], "label": item["label"]} for item in CONTRACT_SUBTYPES],
        "subtype_aliases": dict(sorted(SUBTYPE_ALIASES.items())),
        "maud_consideration_types": list(MAUD_CONSIDERATION_TYPES),
        "subclass_normalization": {doc_type: _subclass_table(doc_type) for doc_type in live},
        "intake": {
            "prep_steps": list(INTAKE_PREP_STEPS),
            "cases": [
                {"name": name, "input": text, "output": out, "stats": stats}
                for name, text in INTAKE_CASES
                for out, stats in (deterministic_normalize(text),)
            ],
        },
    }


def _digest(value: Any) -> str:
    """sha256 of ``json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))``."""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ml_contract() -> dict[str, Any]:
    """Build the contract: schema id, each part, and a sha256 digest per part."""
    parts = _parts()
    return {
        "schema": CONTRACT_SCHEMA,
        **parts,
        "digests": {name: _digest(value) for name, value in parts.items()},
    }


def contract_path() -> Path:
    """Where the committed contract lives inside the installed package."""
    return Path(__file__).with_name("data") / "ml_contract.json"


def load_contract() -> dict[str, Any]:
    """Read the committed contract (what a consumer vendors)."""
    return json.loads(contract_path().read_text(encoding="utf-8"))
