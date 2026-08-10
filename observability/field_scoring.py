"""Deterministic, field-type-aware extraction scoring (GitHub issue #4).

The exact-match-on-extraction approach treats all fields identically, which
is wrong: a date, a dollar amount, a party name, and a free-text clause
summary fail differently and need different normalization before comparison.
This module implements one scoring algorithm per field type:

- ``id``          normalize (uppercase, strip punctuation/whitespace), then
                  exact match — docket numbers, filing/reference numbers.
- ``date``        parse both sides (``dateutil.parser``) to a canonical ISO
                  date, then exact match. "March 3, 2024" == "03/03/2024" ==
                  "2024-03-03" score 1.0; a genuinely wrong date scores 0.
- ``money``       strip currency symbols/commas, parse to float, compare with
                  a small relative/absolute tolerance. Unparseable prose
                  values fall back to fuzzy string matching instead of 0.
- ``name``        normalized fuzzy matching: Jaro-Winkler (jellyfish) +
                  token-set ratio (stdlib difflib) on text normalized to
                  uppercase with corporate suffixes stripped.
- ``free_text``   SQuAD-style token F1 over token sets.
- ``entity_list`` optimal bipartite matching (scipy Hungarian algorithm) over
                  a pairwise similarity matrix, thresholded, then
                  precision/recall/F1 over the matched set — a reordered or
                  off-by-one list scores correctly instead of near-zero.

``name`` and ``free_text`` additionally use embedding cosine similarity
(sentence-transformers, small model) as a SECOND signal that rescues
semantically-correct-but-lexically-distant fields when the string score is
ambiguous. The embedding layer is lazy and degrades gracefully (pure string
scoring) if the model is unavailable.

The per-doc-class field→type mapping is config-driven: ``field_types:`` under
each ``doc_classes:`` entry in ``config/taxonomy.yaml``, with a name-based
heuristic fallback for unmapped fields. Everything here is pure and
backend-agnostic; Langfuse attachment lives in
``observability/langfuse_field_scoring.py``.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration (config/taxonomy.yaml -> field_scoring:)
# ---------------------------------------------------------------------------

_DEFAULT_AMBIGUOUS_BAND = (0.5, 0.85)
_DEFAULT_MATCH_THRESHOLD = 0.6
_DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_EMBEDDING_RESCUE_BELOW = 0.7
_DEFAULT_EMBEDDING_ENABLED = True


def _field_scoring_config() -> dict:
    try:
        from pipeline.config import load_config

        return load_config().get("field_scoring", {}) or {}
    except Exception:
        return {}


def get_ambiguous_band() -> tuple[float, float]:
    band = _field_scoring_config().get("ambiguous_band")
    if isinstance(band, (list, tuple)) and len(band) == 2:
        return (float(band[0]), float(band[1]))
    return _DEFAULT_AMBIGUOUS_BAND


def get_type_bands() -> dict:
    """Per-field-type ambiguous-band overrides from ``field_scoring.type_bands``
    (issue #4 calibration: each field type gets its own cutoff instead of one
    global band). ``"always"`` = every field of that type escalates to the LLM
    judge (no deterministic cutoff exists); ``"never"`` = no field of that type
    ever escalates (the deterministic score is decisive both ways)."""
    tb = _field_scoring_config().get("type_bands") or {}
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
    (fail-safe toward the judge)."""
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


def get_bipartite_match_threshold() -> float:
    val = _field_scoring_config().get("bipartite_match_threshold")
    try:
        return float(val)
    except (TypeError, ValueError):
        return _DEFAULT_MATCH_THRESHOLD


def get_embedding_model() -> str:
    return str(_field_scoring_config().get("embedding_model") or _DEFAULT_EMBEDDING_MODEL)


def get_embedding_rescue_below() -> float:
    val = _field_scoring_config().get("embedding_rescue_below")
    try:
        return float(val)
    except (TypeError, ValueError):
        return _DEFAULT_EMBEDDING_RESCUE_BELOW


def embedding_enabled() -> bool:
    val = _field_scoring_config().get("embedding_enabled", _DEFAULT_EMBEDDING_ENABLED)
    return bool(val)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Corporate / honorific suffixes removed from name tokens before matching
# ("Global Technologies, Ltd" vs "Global Technologies Ltd" — same entity).
_CORPORATE_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LTD", "LIMITED", "CORP", "CORPORATION",
    "CO", "COMPANY", "PLC", "LLP", "LP", "PLLC", "PC", "PA", "ESQ",
    "ESQUIRE", "GMBH", "AG", "NV", "SA", "SARL", "BV", "PTY", "GROUP",
    "HOLDINGS", "TRUST",
}
_PUNCT_RE = re.compile(r"[,.;:'\"()\[\]{}!?@#$%^&*+=|\\/<>~`_-]")
_ORDINAL_RE = re.compile(r"(\d)(st|nd|rd|th)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_text(text) -> str:
    """Uppercase, strip punctuation, drop corporate suffixes, collapse
    whitespace. The canonical form used by exact and fuzzy matching."""
    if not isinstance(text, str):
        text = str(text)
    tokens = _PUNCT_RE.sub(" ", text.upper()).split()
    tokens = [t for t in tokens if t not in _CORPORATE_SUFFIXES]
    return " ".join(tokens)


def _tokenize(text: str) -> list[str]:
    """Lowercase, punctuation-stripped tokens (for F1-style matching)."""
    if not isinstance(text, str):
        text = str(text)
    return _PUNCT_RE.sub(" ", text.lower()).split()


def _seq_ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _token_set_ratio(a: str, b: str) -> float:
    """Token-set ratio (rapidfuzz-style) over space-joined sorted tokens.

    Handles reordering and extra/missing words: "John Smith, Esq." vs
    "Smith, John" vs "John A. Smith" all tokenize to overlapping sets and
    score high regardless of order or duplicates.
    """
    ta, tb = set(_tokenize(a)), set(_tokenize(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if inter == ta and inter == tb:
        return 1.0
    base = _seq_ratio(" ".join(sorted(inter)), " ".join(sorted(ta)))
    diff = _seq_ratio(" ".join(sorted(ta - tb)), " ".join(sorted(tb - ta)))
    return max(base, diff)


def _token_f1(pred: str, exp: str) -> float:
    """SQuAD-style token-level F1 over lowercase token multisets."""
    pt, gt = _tokenize(pred), _tokenize(exp)
    if not gt:
        return 1.0 if not pt else 0.0
    if not pt:
        return 0.0
    common = Counter(gt) & Counter(pt)
    tp = sum(common.values())
    if tp == 0:
        return 0.0
    prec, rec = tp / len(pt), tp / len(gt)
    return 2 * prec * rec / (prec + rec)


# ---------------------------------------------------------------------------
# Scalar field scorers
# ---------------------------------------------------------------------------

def _parse_date(text) -> "object | None":
    """Parse to a canonical datetime.date, or None when unparseable."""
    if not isinstance(text, str):
        return None
    s = _WS_RE.sub(" ", text.strip())
    if not s:
        return None
    s = _ORDINAL_RE.sub(r"\1", s)
    try:
        from dateutil import parser

        dt = parser.parse(s)
        return dt.date()
    except (ValueError, OverflowError):
        return None


def score_id_field(pred, exp, embedding=None) -> float:
    """Normalize (upper, strip punctuation/whitespace), then exact match."""
    np, ne = normalize_text(pred), normalize_text(exp)
    if not np and not ne:
        return 1.0
    if not np or not ne:
        return 0.0
    return 1.0 if np == ne else 0.0


def _parse_money(text) -> float | None:
    """Strip currency symbols/commas, expand K/M/B suffixes, parse to float."""
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    s = text.strip().upper().replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    multiplier = 1.0
    for suffix, m in (("M", 1e6), ("K", 1e3), ("B", 1e9)):
        if s.endswith(suffix):
            multiplier = m
            s = s[:-1].rstrip()
            break
    for tail in (" USD", " DOLLARS", " EUROS"):
        if s.endswith(tail):
            s = s[: -len(tail)].rstrip()
            break
    if not s:
        return None
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def score_money_field(pred, exp, embedding=None) -> float:
    """Numeric parse + tolerance compare; unparseable prose falls back to
    fuzzy string matching instead of scoring 0."""
    pa, ea = _parse_money(pred), _parse_money(exp)
    if pa is not None and ea is not None:
        # One-cent absolute tolerance. Legal amounts are exact: "$250,001"
        # vs "$250,000" is a different value, not rounding noise.
        return 1.0 if abs(pa - ea) <= 0.01 else 0.0
    return score_name_field(pred, exp, embedding=embedding)


def _jaro_winkler(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        import jellyfish

        return float(jellyfish.jaro_winkler_similarity(a, b))
    except ImportError:
        return _seq_ratio(a, b)
    except Exception:
        return _seq_ratio(a, b)


def _with_embedding_rescue(string_score: float, pred, exp, embedding) -> float:
    """Use embedding cosine as a secondary signal only when the string score
    is ambiguous (below the rescue threshold) — never overriding a confident
    string-level reject. Fetches the matcher lazily when not provided."""
    if string_score >= get_embedding_rescue_below():
        return string_score
    if embedding is None:
        embedding = _get_embedding()
    if embedding is None:
        return string_score
    try:
        sim = embedding.similarity(pred, exp)
    except Exception:
        logger.warning("embedding_similarity_failed", exc_info=True)
        return string_score
    if sim is None:
        return string_score
    return max(string_score, float(sim))


def score_name_field(pred, exp, embedding=None) -> float:
    """Normalized fuzzy matching: max of Jaro-Winkler and token-set ratio,
    with embedding rescue for lexically distant but semantically equal
    names."""
    np, ne = normalize_text(pred), normalize_text(exp)
    if not np and not ne:
        return 1.0
    if not np or not ne:
        return 0.0
    base = max(_jaro_winkler(np, ne), _token_set_ratio(np, ne))
    return _with_embedding_rescue(base, pred, exp, embedding)


def score_free_text_field(pred, exp, embedding=None) -> float:
    """SQuAD-style token F1, with embedding rescue for paraphrases."""
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
    f1 = _token_f1(pred, exp)
    return _with_embedding_rescue(f1, pred, exp, embedding)


def score_date_field(pred, exp, embedding=None) -> float:
    """Parse to canonical date, then exact match. Unparseable values fall
    back to fuzzy string matching."""
    dp, de = _parse_date(pred), _parse_date(exp)
    if dp is not None and de is not None:
        return 1.0 if dp == de else 0.0
    return score_name_field(pred, exp, embedding=embedding)


# ---------------------------------------------------------------------------
# Entity LIST scoring — optimal bipartite matching (relaxed NER style)
# ---------------------------------------------------------------------------

@dataclass
class EntityListScore:
    field_name: str
    precision: float
    recall: float
    f1: float
    matched: int
    unmatched_predicted: int
    unmatched_expected: int


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _element_scorer(element_type: str):
    """Scalar scorer for entity-list elements (entity_list:<element_type>)."""
    return FIELD_SCORERS.get(element_type, score_name_field)


def score_entity_list(element_type: str, pred, exp, embedding=None) -> EntityListScore:
    """Pairwise similarity matrix + Hungarian assignment (scipy), thresholded,
    then precision/recall/F1 over the matched set."""
    pred_items = [str(item) for item in _as_list(pred)]
    exp_items = [str(item) for item in _as_list(exp)]
    if not pred_items and not exp_items:
        return EntityListScore("", 1.0, 1.0, 1.0, 0, 0, 0)
    if not pred_items:
        return EntityListScore("", 0.0, 1.0, 0.0, 0, 0, len(exp_items))
    if not exp_items:
        return EntityListScore("", 1.0, 0.0, 0.0, 0, len(pred_items), 0)

    scorer = _element_scorer(element_type)
    threshold = get_bipartite_match_threshold()
    n_pred, n_exp = len(pred_items), len(exp_items)

    # Skip the Hungarian machinery when a single-element list is trivially
    # scored — cheaper and identical for the common one-name lists.
    if n_pred == 1 and n_exp == 1:
        matched = 1 if scorer(pred_items[0], exp_items[0], embedding) >= threshold else 0
    else:
        try:
            import numpy as np
            from scipy.optimize import linear_sum_assignment

            sim = np.array([
                [scorer(p, e, embedding) for e in exp_items] for p in pred_items
            ])
            row_idx, col_idx = linear_sum_assignment(1.0 - sim)
            matched = sum(1 for r, c in zip(row_idx, col_idx) if sim[r, c] >= threshold)
        except Exception:
            # scipy unavailable/failed: greedy one-to-one assignment fallback.
            logger.warning("bipartite_matching_failed", fallback="greedy", exc_info=True)
            matched = 0
            assigned_exp = set()
            for p_item in pred_items:
                best, best_e = threshold, None
                for ei, e_item in enumerate(exp_items):
                    if ei in assigned_exp:
                        continue
                    s = scorer(p_item, e_item, embedding)
                    if s >= best:
                        best, best_e = s, ei
                if best_e is not None:
                    matched += 1
                    assigned_exp.add(best_e)

    precision = matched / n_pred
    recall = matched / n_exp
    f1 = 2 * precision * recall / (precision + recall) if matched else 0.0
    return EntityListScore(
        field_name="",
        precision=precision,
        recall=recall,
        f1=f1,
        matched=matched,
        unmatched_predicted=n_pred - matched,
        unmatched_expected=n_exp - matched,
    )


# ---------------------------------------------------------------------------
# Embedding second signal (sentence-transformers, lazy)
# ---------------------------------------------------------------------------

class _EmbeddingMatcher:
    """Lazy singleton embedding cache. similarity() returns None whenever the
    model is unavailable — callers then keep the string-only score."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "_EmbeddingMatcher":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._model = None
        self._vectors: dict[str, "object"] = {}

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(get_embedding_model())

    def similarity(self, a: str, b: str) -> float | None:
        if not embedding_enabled():
            return None
        try:
            if self._model is None:
                self._load()
            import numpy as np

            va = self._vectors.get(a)
            if va is None:
                va = self._model.encode([a], normalize_embeddings=True)[0]
                self._vectors[a] = va
            vb = self._vectors.get(b)
            if vb is None:
                vb = self._model.encode([b], normalize_embeddings=True)[0]
                self._vectors[b] = vb
            sim = float(np.dot(va, vb))
            return min(1.0, max(0.0, sim))
        except Exception:
            return None


def _get_embedding() -> "_EmbeddingMatcher | None":
    if not embedding_enabled():
        return None
    try:
        return _EmbeddingMatcher.get()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatch + composite scoring
# ---------------------------------------------------------------------------

FIELD_SCORERS = {
    "id": score_id_field,
    "date": score_date_field,
    "money": score_money_field,
    "name": score_name_field,
    "free_text": score_free_text_field,
}

# List-valued field types whose element scorer is specified as a suffix:
# "entity_list:name", "entity_list:free_text", "entity_list:id", ...
LIST_PREFIX = "entity_list"


def is_entity_list(field_type: str) -> bool:
    return field_type == LIST_PREFIX or field_type.startswith(LIST_PREFIX + ":")


def score_field(field_type: str, pred, exp, embedding=None):
    """Score one field. Returns a float, or an EntityListScore for list fields."""
    if is_entity_list(field_type):
        element_type = field_type.split(":", 1)[1] if ":" in field_type else "name"
        return score_entity_list(element_type, pred, exp, embedding=embedding)
    scorer = FIELD_SCORERS.get(field_type, score_name_field)
    return scorer(pred, exp, embedding=embedding)


def _heuristic_field_type(field_name: str, value) -> str:
    """Fallback type inference for fields not mapped in taxonomy.yaml."""
    if isinstance(value, list):
        return LIST_PREFIX
    name = field_name.lower()
    if "date" in name:
        return "date"
    if any(k in name for k in ("value", "amount", "fee", "compensation", "price", "cost", "salary", "consideration", "total")):
        return "money"
    if any(k in name for k in ("number", "id", "docket", "reference", "filing")):
        return "id"
    return "name"


def get_field_types(doc_class: str) -> dict[str, str]:
    """Field→scoring-type mapping for a doc class, straight from
    config/taxonomy.yaml (`doc_classes[].field_types`)."""
    try:
        from pipeline.config import load_config

        for cls in load_config().get("doc_classes", []):
            if cls.get("key") == doc_class:
                return dict(cls.get("field_types") or {})
    except Exception:
        logger.warning("field_types_unavailable", doc_class=doc_class)
    return {}


@dataclass
class ExtractionScoreResult:
    doc_class: str
    field_scores: dict[str, float]
    overall_score: float | None
    ambiguous_fields: list[str]
    entity_list_scores: dict[str, EntityListScore] = field(default_factory=dict)

    @property
    def needs_judge_review(self) -> bool:
        return bool(self.ambiguous_fields)


def score_extraction(
    doc_class: str,
    field_types: dict[str, str],
    predicted: dict | None,
    expected: dict | None,
) -> ExtractionScoreResult:
    """Score one extraction deterministically.

    - Only expected fields with a non-null/non-empty value count toward the
      overall score (null expectations are not requirements).
    - ``overall_score`` is the mean of the per-field scores (None when no
      field is scored).
    - ``ambiguous_fields`` collects fields landing in the ambiguous band
      (``field_scoring.ambiguous_band``, or the per-type override in
      ``field_scoring.type_bands``) — the signal that escalates to the
      LLM judge.
    - List fields also produce ``entity_list_scores`` with precision/recall.
    """
    predicted = predicted or {}
    expected = expected or {}
    needs_embedding = any(
        ft in ("name", "free_text") or (is_entity_list(ft) and (ft.split(":", 1)[1] if ":" in ft else "name") in ("name", "free_text"))
        for ft in field_types.values()
    )
    embedding = _get_embedding() if needs_embedding else None

    field_scores: dict[str, float] = {}
    ambiguous: list[str] = []
    entity_list_scores: dict[str, EntityListScore] = {}

    for key, exp_value in expected.items():
        if exp_value is None or exp_value == "":
            continue
        field_type = field_types.get(key) or _heuristic_field_type(key, exp_value)
        pred_value = predicted.get(key)
        if pred_value is None:
            score = 0.0
        else:
            result = score_field(field_type, pred_value, exp_value, embedding=embedding)
            if isinstance(result, EntityListScore):
                result.field_name = key
                entity_list_scores[key] = result
                score = result.f1
            else:
                score = result
        score = round(score, 4)
        field_scores[key] = score
        if field_is_ambiguous(field_type, score):
            ambiguous.append(key)

    overall = round(sum(field_scores.values()) / len(field_scores), 4) if field_scores else None
    return ExtractionScoreResult(
        doc_class=doc_class,
        field_scores=field_scores,
        overall_score=overall,
        ambiguous_fields=ambiguous,
        entity_list_scores=entity_list_scores,
    )
