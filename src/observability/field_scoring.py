"""DEPRECATED shim — the field-scoring implementation moved to the package.

As of KANBAN-061 (entity-extraction issue #27, llm-dojo-scoring v0.5.0) this
module is a thin backward-compatibility layer: the ~1,200 lines of scoring
logic that used to live here are now maintained ONCE in
``llm_dojo_scoring.field_scoring`` (same functions, same semantics — the copy
was function-for-function identical).

What still lives HERE (mailroom-local glue, not package concerns):

- ``get_type_bands`` / ``field_is_ambiguous`` — the per-field-type
  ambiguous-band overrides from ``config/taxonomy.yaml``
  (``field_scoring.type_bands``), including the ``always``/``never`` modes.
- ``warm_embedding_model`` — off-document-path embedding preload (O-10).
- taxonomy wiring — this module maps ``config/taxonomy.yaml``'s
  ``field_scoring:`` block onto the package ``Settings`` and calls
  ``llm_dojo_scoring.configure()`` once at import, so the package functions
  honor mailroom's calibrated thresholds (entity-extraction does the same in
  its ``src/dojo_config.py``).

Importing this module emits a ``DeprecationWarning``: switch imports to
``llm_dojo_scoring.field_scoring`` (plus the three glue functions below while
they remain mailroom-only). The module will be deleted in a future release.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "observability.field_scoring is deprecated since KANBAN-061: the scoring "
    "implementation lives in llm_dojo_scoring.field_scoring (v0.5.0). Import "
    "from the package; only get_type_bands/field_is_ambiguous/"
    "warm_embedding_model remain mailroom-local.",
    DeprecationWarning,
    stacklevel=2,
)

# ---------------------------------------------------------------------------
# Taxonomy -> package Settings wiring (mailroom's calibrated thresholds)
# ---------------------------------------------------------------------------


def _apply_taxonomy_settings() -> None:
    """Map ``config/taxonomy.yaml`` ``field_scoring:`` onto package Settings.

    Best-effort, mirroring the old module's behavior: any failure (config
    loader unavailable, malformed block) leaves the package defaults in place.
    """
    try:
        from pipeline.config import load_config

        cfg = (load_config().get("field_scoring") or {})
    except Exception:
        return
    if not cfg:
        return

    from llm_dojo_scoring import configure

    fs = cfg.get("factuality_verification") or {}
    # configure() sets values VERBATIM (no coercion), so mirror
    # llm-entity-extraction/src/dojo_config.py: YAML lists become the
    # tuple/set forms the package stores.
    overrides: dict[str, object] = {}
    band = cfg.get("ambiguous_band")
    if isinstance(band, (list, tuple)) and len(band) == 2:
        overrides["field_scoring__ambiguous_band"] = (float(band[0]), float(band[1]))
    if cfg.get("bipartite_match_threshold") is not None:
        overrides["field_scoring__bipartite_match_threshold"] = float(
            cfg["bipartite_match_threshold"]
        )
    if "embedding_enabled" in cfg:
        overrides["field_scoring__embedding_enabled"] = bool(cfg["embedding_enabled"])
    if cfg.get("embedding_model"):
        overrides["field_scoring__embedding_model"] = str(cfg["embedding_model"])
    if cfg.get("embedding_rescue_below") is not None:
        overrides["field_scoring__embedding_rescue_below"] = float(
            cfg["embedding_rescue_below"]
        )
    pf = cfg.get("partial_gt_fields")
    if isinstance(pf, (list, tuple)) and pf:
        overrides["field_scoring__partial_gt_fields"] = set(pf)
    cf = cfg.get("containment_fields")
    if isinstance(cf, (list, tuple)) and cf:
        overrides["field_scoring__containment_fields"] = set(cf)
    overrides["field_scoring__verification_enabled"] = bool(fs.get("enabled", True))
    if fs.get("token_coverage") is not None:
        overrides["field_scoring__verification_token_coverage"] = float(
            fs["token_coverage"]
        )

    if overrides:
        configure(**overrides)


_apply_taxonomy_settings()

# ---------------------------------------------------------------------------
# Re-exports from the package (the implementation)
# ---------------------------------------------------------------------------

from llm_dojo_scoring.field_scoring import (  # noqa: E402,F401
    EntityListScore,
    ExtractionScoreResult,
    FIELD_SCORERS,
    LIST_PREFIX,
    audit_list_field,
    audit_scalar_field,
    disaggregate_clause_spans,
    embedding_enabled,
    get_ambiguous_band,
    get_bipartite_match_threshold,
    get_containment_fields,
    get_embedding_model,
    get_embedding_rescue_below,
    get_field_types as _package_get_field_types,
    get_partial_gt_fields,
    get_presence_embedding_threshold,
    get_verification_coverage,
    is_entity_list,
    normalize_text,
    parse_date,
    parse_money,
    score_category_presence,
    score_containment_field,
    score_date_field,
    score_entity_list,
    score_extraction,
    score_field,
    score_free_text_field,
    score_id_field,
    score_money_field,
    score_name_field,
    verify_list_items,
)
from llm_dojo_scoring.field_scoring import (  # noqa: E402,F401
    # private-but-stable seams the test suite patches/asserts through
    _get_embedding,
    _heuristic_field_type,
)

# ---------------------------------------------------------------------------
# Mailroom-local glue (ported verbatim from the pre-0.5.0 local copy)
# ---------------------------------------------------------------------------


def get_field_types(doc_class: str, taxonomy: dict | None = None) -> dict[str, str]:
    """Field→scoring-type mapping, auto-loading mailroom's taxonomy.

    The package helper requires an explicit taxonomy dict (returns ``{}``
    without one); mailroom's historical behavior auto-loads
    ``config/taxonomy.yaml``, which production callers (and the tests)
    rely on. An explicit ``taxonomy`` argument still wins.
    """
    if taxonomy is not None:
        return _package_get_field_types(doc_class, taxonomy)
    try:
        from pipeline.config import load_config

        return _package_get_field_types(doc_class, load_config())
    except Exception:
        return {}


def get_type_bands() -> dict:
    """Per-field-type ambiguous-band overrides from ``field_scoring.type_bands``.

    ``"always"`` = every field of that type escalates to the LLM judge (no
    deterministic cutoff exists); ``"never"`` = no field of that type ever
    escalates (the deterministic score is decisive both ways).
    """
    try:
        from pipeline.config import load_config

        tb = (load_config().get("field_scoring") or {}).get("type_bands") or {}
    except Exception:
        return {}
    out: dict[str, tuple] = {}
    for k, v in tb.items():
        if v == "always":
            out[k] = ("always",)
        elif v == "never":
            out[k] = ("never",)
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            out[k] = (float(v[0]), float(v[1]))
    return out


def field_is_ambiguous(field_type: str, score: float) -> bool:
    """Is this field score in the (possibly type-specific) ambiguous band?

    Band check is half-open (``low <= score < high``): a perfect score of 1.0
    is never ambiguous, and a score exactly at the low cutoff still escalates
    (fail-safe toward the judge).
    """
    bands = get_type_bands()
    band = bands.get(field_type) or bands.get(field_type.split(":", 1)[0])
    if band == ("always",):
        return True
    if band == ("never",):
        return False
    if band is not None:
        low, high = band
        return low <= score < high
    low, high = get_ambiguous_band()
    return low <= score < high


def warm_embedding_model(blocking: bool = False) -> None:
    """Load the embedding model OFF the document path (O-10).

    The first grounded run that needs a name/free-text embedding used to
    trigger a synchronous multi-minute SentenceTransformer download inside run
    finalization. This kicks the load into a background thread at process
    start instead. Failures are logged; scoring keeps the string-only score.
    """
    import logging
    import threading

    if not embedding_enabled():
        return

    from llm_dojo_scoring.field_scoring import _get_embedding

    log = logging.getLogger(__name__)

    def _run() -> None:
        try:
            _get_embedding()
        except Exception as exc:  # scoring degrades to string-only
            log.warning("embedding model warmup failed: %s", exc)

    if blocking:
        _run()
    else:
        threading.Thread(target=_run, daemon=True, name="embedding-warmup").start()


__all__ = [
    # package re-exports
    "EntityListScore", "ExtractionScoreResult", "FIELD_SCORERS", "LIST_PREFIX",
    "audit_list_field", "audit_scalar_field", "disaggregate_clause_spans",
    "embedding_enabled", "get_ambiguous_band", "get_bipartite_match_threshold",
    "get_containment_fields", "get_embedding_model", "get_embedding_rescue_below",
    "get_field_types", "get_partial_gt_fields", "get_presence_embedding_threshold",
    "get_verification_coverage", "is_entity_list", "normalize_text", "parse_date",
    "parse_money", "score_category_presence", "score_containment_field",
    "score_date_field", "score_entity_list", "score_extraction", "score_field",
    "score_free_text_field", "score_id_field", "score_money_field",
    "score_name_field", "verify_list_items",
    # mailroom-local glue
    "get_type_bands", "field_is_ambiguous", "warm_embedding_model",
]
