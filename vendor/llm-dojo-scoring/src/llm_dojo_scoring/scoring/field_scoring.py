"""Deterministic, field-type-aware extraction scoring.

Ported from ``src/field_scoring.py`` (llm-entity-extraction, itself ported
from llm-mailroom) into the shared suite so both projects import ONE scoring
implementation. The exact-match-on-extraction approach treats all fields
identically, which is wrong: a date, a dollar amount, a party name, and a
free-text clause summary fail differently and need different normalization
before comparison. This module implements one scoring algorithm per field
type:

- ``id``          normalize (uppercase, strip punctuation/whitespace), then
                  exact match.
- ``date``        parse both sides to a canonical ISO date, then exact match
                  with containment + partial-credit fallbacks.
- ``money``       strip currency symbols/commas, parse to float, compare with
                  a one-cent tolerance; unparseable prose falls back to fuzzy
                  string matching.
- ``name``        normalized fuzzy matching: Jaro-Winkler (jellyfish) +
                  token-set ratio (stdlib difflib), with containment first.
- ``free_text``   SQuAD-style token F1 over token multisets.
- ``entity_list`` optimal bipartite matching (scipy Hungarian algorithm) over
                  a pairwise similarity matrix, thresholded, then
                  precision/recall/F1 over the matched set.

``name`` and ``free_text`` optionally use embedding cosine similarity as a
SECOND signal that rescues semantically-correct-but-lexically-distant fields
when the string score is ambiguous. The embedding layer is lazy and degrades
gracefully (pure string scoring) if unavailable.

All thresholds and field sets come from :mod:`llm_dojo_scoring.config`
(``Settings.field_scoring``) — override via YAML or ``configure(...)``.
"""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

import structlog

from ..config import get_settings
from ..schemas.corpus import CMS_DOCUMENTED_ABSENT_FIELDS, CMS_SOURCE_TABLES
from ..schemas.gt_metadata import ANNOTATION_KEYS, is_empty_value, parse_json_container
from ..schemas.intents import INTENT_LABELS, normalize_intent  # noqa: F401  (historic module attributes)
from ..schemas.scored_enums import canonical_label

logger = structlog.get_logger(__name__)

#: Trace / format artifacts and Hub annotation stats — never enter
#: :func:`score_extraction` (mailroom-issues #237 / #238).
NEVER_SCORED_FIELDS: frozenset[str] = frozenset(
    {"confidence", "reasoning"}
) | ANNOTATION_KEYS

#: Prompt-catalog keys retired from live models. Ignored unless the caller
#: passed them in an explicit ``field_types`` map (historical rescoring).
RETIRED_PROMPT_KEYS: frozenset[str] = frozenset(
    {
        "key_obligations",
        "termination_clauses",
        "key_provisions",
        "key_points",
        "referenced_communications",
    }
)

# ---------------------------------------------------------------------------
# Configuration accessors (kept for drop-in compatibility with the
# llm-entity-extraction API — all read the shared settings object)
# ---------------------------------------------------------------------------


#: CMS DE-SynPUF table tokens. They are a *source table*, not a product line:
#: the published merge labels every CMS row ``claim_type=health`` and the table
#: lives in its own field, ``cms_source_table`` (dojo #93). Before #93 a table
#: token predicted on ``claim_type`` was folded onto ``health`` (the SAND-042
#: equivalence); that fold is retired for scoring.
CMS_CLAIM_TYPES: frozenset[str] = frozenset({"pde", "inpatient", "outpatient", "carrier"})

#: Field types the scorer applies on a CMS row, explicitly (never an implicit
#: heuristic): the derived table is a controlled ``label``; supporting
#: documents are identifier strings (``provider NPI 5040521237``,
#: ``NDC 00703051002``, ``HCPCS detail (1 codes)``) compared by
#: :func:`score_cms_doc_ref`. Not part of ``DEFAULT_FIELD_TYPES``, which
#: mirrors the mailroom taxonomy and is pinned to it.
CMS_ROW_FIELD_TYPES: dict[str, str] = {
    # Closed product-line vocabulary on a CMS row (GT is always ``health``):
    # exact match, so a table token earns no fuzzy name credit.
    "claim_type": "label",
    "cms_source_table": "label",
    "supporting_documents": "entity_list:cms_doc_ref",
}


def canonical_claim_type(predicted: Any, expected: Any) -> Any:
    """Legacy SAND-042 fold: a CMS table token onto ``health`` when GT is ``health``.

    **No longer used by the scorer (dojo #93).** ``claim_type`` (product line)
    and ``cms_source_table`` are separate fields, so a table token predicted
    on ``claim_type`` scores as wrong there; ``score_extraction`` does not call
    this. The name stays importable for consumers that analysed stored runs
    under the old equivalence. Behaviour is unchanged: only the CMS -> health
    direction folds.
    """
    pred = str(predicted).strip().lower()
    if pred in CMS_CLAIM_TYPES and str(expected).strip().lower() == "health":
        return "health"
    return predicted


def expected_cms_source_table(doc_class: str | None, expected: Any) -> str | None:
    """The CMS source table an expected row carries, else ``None``.

    This is what makes a row a CMS row for scoring: the table is derived from
    ``expected_subclass`` (``scoring_gt_fields`` / ``cms_source_table``) and
    injected into the expected fields, so every scoring path that assembles GT
    that way shares one definition. Non-CMS rows never carry it.
    """
    if doc_class != "insurance_claim" or not isinstance(expected, dict):
        return None
    token = str(parse_json_container(expected.get("cms_source_table")) or "").strip().lower()
    if token not in CMS_SOURCE_TABLES:
        return None
    # Every published CMS row is ``claim_type=health``. A row whose GT names
    # another product line is not CMS, whatever stray key it carries.
    claim_type = parse_json_container(expected.get("claim_type"))
    if not is_empty_value(claim_type) and str(claim_type).strip().lower() != "health":
        return None
    return token


def get_ambiguous_band() -> tuple[float, float]:
    """Return the configured global half-open judge-review score band."""
    return get_settings().field_scoring.ambiguous_band


def get_bipartite_match_threshold() -> float:
    """Return the minimum similarity used for bipartite entity matching."""
    return get_settings().field_scoring.bipartite_match_threshold


def get_embedding_model() -> str:
    """Return the configured local sentence-transformer model name."""
    return get_settings().field_scoring.embedding_model


def get_embedding_rescue_below() -> float:
    """Return the string-score threshold below which embedding rescue is attempted."""
    return get_settings().field_scoring.embedding_rescue_below


def get_presence_embedding_threshold() -> float:
    """Minimum embedding cosine for a disaggregated clause span to satisfy a
    CUAD YES/NO category-presence expectation (issue #21)."""
    return get_settings().field_scoring.presence_embedding_threshold


def embedding_enabled() -> bool:
    """Return whether embedding-based scoring is enabled in settings."""
    return get_settings().field_scoring.embedding_enabled


def get_type_bands() -> dict[str, tuple]:
    """Per-field-type ambiguous-band overrides from the configured taxonomy.

    Values are canonical tuples:
      ``("always",)`` — every field of that type escalates to the LLM judge
      ``("never",)``  — no field of that type ever escalates (the
                        deterministic score is decisive both ways)
      ``(low, high)`` — half-open band check (``low <= score < high``)

    Populated by ``configure_from_taxonomy()``; empty (fall back to the
    global ``ambiguous_band``) when no taxonomy was wired.
    """
    return dict(get_settings().field_scoring.type_bands)


def field_is_ambiguous(field_type: str, score: float) -> bool:
    """Is this field score in the (possibly type-specific) ambiguous band?

    Checks the configured ``type_bands`` first (matching on the exact field
    type or its ``namespace:name`` prefix), then falls back to the global
    ``ambiguous_band``. Band check is half-open (``low <= score < high``): a
    perfect score of 1.0 is never ambiguous, and a score exactly at the low
    cutoff still escalates (fail-safe toward the judge).
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

    No-op when embedding scoring is disabled (``embedding_enabled()`` false).
    """
    import logging
    import threading

    if not embedding_enabled():
        return

    log = logging.getLogger(__name__)

    def _run() -> None:
        """Warm the embedding matcher, logging failures without interrupting scoring."""
        try:
            _get_embedding()
        except Exception as exc:  # scoring degrades to string-only
            log.warning("embedding model warmup failed: %s", exc)

    if blocking:
        _run()
    else:
        threading.Thread(target=_run, daemon=True, name="embedding-warmup").start()


def get_partial_gt_fields() -> set[str]:
    """Field names whose CUAD-style ground truth is a PARTIAL sample of the
    document's content (QA-answer snippets), not an exhaustive list. For these
    fields the model's extraction is usually MORE complete than the label set,
    so they are scored by GROUND-TRUTH COVERAGE (recall) instead of F1."""
    return set(get_settings().field_scoring.partial_gt_fields)


def get_containment_fields() -> set[str]:
    """Field names scored by EXPECTED-WITHIN-PREDICTED token containment."""
    return set(get_settings().field_scoring.containment_fields)


def verification_enabled() -> bool:
    """Whether predicted items are verified against the source document text
    (the factuality guard)."""
    return get_settings().field_scoring.verification_enabled


def get_verification_coverage() -> float:
    """Minimum normalized-token coverage of a predicted item within the
    source document text for it to count as grounded (not hallucinated)."""
    return get_settings().field_scoring.verification_token_coverage


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


# Function words excluded from CONTAINMENT token sets only. Clause labels
# ("the laws of the State of Delaware") carry them; counting them inflates
# misses on short paraphrases and under-flags ambiguity.
_CONTAINMENT_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "by", "as",
    "is", "be", "are", "with", "its", "their", "his", "her", "such", "hereof",
    "hereunder", "shall", "any", "all", "this", "that", "these", "those",
    "from", "between", "into", "upon", "under", "over",
}


def _containment_tokens(text: str) -> set[str]:
    """Normalized token set for containment scoring (stopwords removed)."""
    return {t for t in _tokenize(text) if t not in _CONTAINMENT_STOPWORDS}


def _seq_ratio(a: str, b: str) -> float:
    """Return sequence similarity, scoring two empty strings as an exact match."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _token_set_ratio(a: str, b: str) -> float:
    """Token-set ratio (rapidfuzz-style) over space-joined sorted tokens."""
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

_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]


def _parse_date(text) -> Optional[Any]:
    """Parse to a canonical datetime.date, or None when unparseable.

    Handles the forms that appear in CUAD ground truth: ISO, mm/dd/yyyy,
    "March 3, 2024", ordinal prose ("10th day of January 2000"), and stray
    trailing artifacts like "1st day of April, 2007 (".
    """
    if not isinstance(text, str):
        return None
    s = _WS_RE.sub(" ", text.strip())
    if not s:
        return None
    s = _ORDINAL_RE.sub(r"\1", s)
    s = re.sub(
        r"\b(\d{1,2})[a-z]*(?:\s+[a-z]+)*\s+day\s+of\b", r"\1 day of", s,
        flags=re.IGNORECASE,
    )
    day_of_month = re.search(
        r"\b(\d{1,2})\s+day\s+of\s+([A-Za-z]+)[,\s]*(\d{4})?", s, flags=re.IGNORECASE
    )
    if day_of_month:
        month, day, year = day_of_month.group(2), day_of_month.group(1), day_of_month.group(3)
        s = f"{month} {day}, {year}" if year else f"{month} {day}"
    s = re.sub(r"[(\[\{,]+$", "", s).strip()
    try:
        from dateutil import parser

        dt = parser.parse(s)
        return dt.date()
    except (ValueError, OverflowError):
        return None


def parse_date(text) -> Optional[Any]:
    """Public alias of :func:`_parse_date` — canonical datetime.date for any
    CUAD-style date string, or None when unparseable."""
    return _parse_date(text)


def _parse_money(text) -> Optional[float]:
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


def parse_money(text) -> Optional[float]:
    """Public alias of :func:`_parse_money` — canonical USD float for any
    money-style string, or None when unparseable."""
    return _parse_money(text)


def score_id_field(pred, exp, embedding=None) -> float:
    """Normalize (upper, strip punctuation/whitespace), then exact match."""
    np_, ne = normalize_text(pred), normalize_text(exp)
    if not np_ and not ne:
        return 1.0
    if not np_ or not ne:
        return 0.0
    return 1.0 if np_ == ne else 0.0


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
    """Return Jaro-Winkler similarity, falling back to sequence matching on failure."""
    if not a or not b:
        return 0.0
    try:
        import jellyfish

        return float(jellyfish.jaro_winkler_similarity(a, b))
    except Exception:
        return _seq_ratio(a, b)


def _with_embedding_rescue(string_score: float, pred, exp, embedding) -> float:
    """Use embedding cosine as a second signal only when the string score is
    ambiguous (below the rescue threshold) — never overriding a confident
    string-level reject. Fetches the matcher lazily when not provided."""
    if string_score >= get_embedding_rescue_below():
        return string_score
    if not str(pred).strip() or not str(exp).strip():
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
    """Normalized fuzzy matching: containment first, then max of Jaro-Winkler
    (only when the names share a token) and token-set ratio, with embedding
    rescue for lexically distant but semantically equal names."""
    np_, ne = normalize_text(pred), normalize_text(exp)
    if not np_ and not ne:
        return 1.0
    if not np_ or not ne:
        return 0.0
    if set(_tokenize(ne)) and set(_tokenize(ne)) <= set(_tokenize(np_)):
        return 1.0
    base = _token_set_ratio(np_, ne)
    if set(_tokenize(np_)) & set(_tokenize(ne)):
        base = max(base, _jaro_winkler(np_, ne))
    return _with_embedding_rescue(base, pred, exp, embedding)


def score_label_field(pred, exp, embedding=None) -> float:
    """Exact match on a controlled label: 1.0 when both sides compact
    (lowercase, alphanumerics only) to the same non-empty token, else 0.0.

    No partial credit — a wrong label is a miss, not a near-miss. Callers
    canonicalize aliases first (see ``score_extraction`` / ``intents``).
    """
    cp = re.sub(r"[^a-z0-9]", "", str(pred or "").lower())
    ce = re.sub(r"[^a-z0-9]", "", str(exp or "").lower())
    return 1.0 if cp and cp == ce else 0.0


def score_free_text_field(pred, exp, embedding=None) -> float:
    """SQuAD-style token F1, with embedding rescue for paraphrases."""
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
    f1 = _token_f1(pred, exp)
    return _with_embedding_rescue(f1, pred, exp, embedding)


def score_containment_field(pred, exp, embedding=None) -> float:
    """Share of the EXPECTED text's normalized tokens covered by the
    prediction (expected-within-predicted containment)."""
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
    te = _containment_tokens(exp)
    tp = _containment_tokens(pred)
    if not te:
        return 1.0 if not tp else 0.0
    if not tp:
        return 0.0
    return round(len(te & tp) / len(te), 4)


def score_date_field(pred, exp, embedding=None) -> float:
    """Parse to canonical date, then exact match — with containment and
    partial-credit fallbacks.

    Rules, in order:

    1. Exact parsed-date equality -> 1.0 (the discriminator).
    2. Containment -> 1.0: the labeler's date phrase appears inside the
       prediction (or vice versa).
    3. Partial credit by shared components when both parse: year+month ->
       0.67, within 45 days -> 0.67, year-only -> 0.33.
    4. Unparseable values fall back to fuzzy string matching.
    """
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
    # Blank-template and label-only ground truth (CUAD annotators select the
    # contract's literal date line: "_____ day of ________, 19____",
    # "Effective Date:") hold NO actual date. The model answering null is
    # CORRECT, and a fabricated date is not the labeled date.
    if _parse_date(exp) is None and _date_expected_is_null(exp):
        return 1.0 if not str(pred).strip() else 0.0
    if _date_phrase_contained(pred, exp) or _date_phrase_contained(exp, pred):
        return 1.0
    dp, de = _parse_date(pred), _parse_date(exp)
    if dp is not None and de is not None:
        if dp == de:
            return 1.0
        shared = sum(1 for a, b in ((dp.year, de.year), (dp.month, de.month)) if a == b)
        if shared == 2:
            return 0.67
        if abs((dp - de).days) <= 45:
            return 0.67
        if shared == 1:
            return 0.33
        return 0.0
    return score_name_field(pred, exp, embedding=embedding)


def _date_expected_is_null(exp: str) -> bool:
    """True when the expected date text carries no real date: a blank
    template line or a bare label."""
    if "_" in exp or "blank" in exp.lower():
        return True
    low = exp.lower()
    if not any(m in low for m in _MONTH_NAMES):
        if not re.search(r"\b(19|20)\d{2}\b", low):
            return True
    return False


def _date_phrase_contained(container: str, target: str) -> bool:
    """True when a date phrase stated in ``target`` appears in ``container``."""
    def norm(text: str) -> str:
        """Lowercase a date phrase and collapse non-alphanumeric separators to spaces."""
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text).lower())).strip()

    nc = norm(container)
    if not nc:
        return False
    target_norm = norm(target)
    if not target_norm:
        return False
    dt = _parse_date(target)
    explicit_month = (
        any(f" {month}" in target_norm for month in _MONTH_NAMES)
        or bool(re.search(r"\b\d{4}-\d{1,2}\b", target_norm))
    )
    if target_norm in nc and (len(target_norm.split()) >= 3 or explicit_month):
        return True
    if dt is None:
        return False
    month = _MONTH_NAMES[dt.month - 1]
    renderings = [
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}",
        f"{month} {dt.day} {dt.year}",
        f"{dt.day} {month} {dt.year}",
    ]
    return any(norm(r) in nc for r in renderings)


# ---------------------------------------------------------------------------
# Entity LIST scoring — optimal bipartite matching (relaxed NER style)
# ---------------------------------------------------------------------------


@dataclass
class EntityListScore:
    """Entity-list matching counts and precision, recall, and F1 for one field."""
    field_name: str
    precision: float
    recall: float
    f1: float
    matched: int
    unmatched_predicted: int
    unmatched_expected: int
    partial_gt: bool = False

    @property
    def score(self) -> float:
        """Composite score: F1 normally; GROUND-TRUTH COVERAGE (recall) when
        the field's label set is a partial sample (partial_gt_fields)."""
        return self.recall if self.partial_gt else self.f1

    def to_dict(self) -> dict:
        """Serialize rounded list metrics and reconstruct predicted and expected counts."""
        return {
            "field": self.field_name,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "matched": self.matched,
            "n_predicted": self.unmatched_predicted + self.matched,
            "n_expected": self.unmatched_expected + self.matched,
            "partial_gt": self.partial_gt,
        }


def _as_list(value) -> list:
    """Parse a JSON container, then return a list or wrap a non-list value.

    ``None`` becomes ``[]``; existing lists are returned unchanged.
    """
    value = parse_json_container(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _element_scorer(element_type: str):
    """Scalar scorer for entity-list elements (entity_list:<element_type>)."""
    return FIELD_SCORERS.get(element_type, score_name_field)


# Party ROLE labels that CUAD ground truth uses INSTEAD of the actual entity
# name ("Shipper.", "Sponsor", "Seller", ...). A role word in the label set is
# matched whenever the prediction names at least one party.
_ROLE_WORDS = {
    "shipper", "sponsor", "seller", "buyer", "company", "producer", "reseller",
    "licensee", "licensor", "party", "parties", "transporter", "customer",
    "client", "vendor", "purchaser", "lender", "borrower", "guarantor",
    "manufacturer", "distributor", "owner", "operator",
}


def _is_role_word(text: str) -> bool:
    """Recognize short phrases consisting entirely of generic party-role words."""
    tokens = _tokenize(str(text))
    return 1 <= len(tokens) <= 3 and all(t in _ROLE_WORDS for t in tokens)


def _element_similarity(element_type: str, item: str, exp: str, embedding=None) -> float:
    """Similarity of one predicted list item against one ground-truth item.

    For ``free_text`` elements, when the item covers >= the verification
    coverage of the answer's plain tokens, that coverage is the similarity
    (the item IS the labeled clause, quoted at length)."""
    base = _element_scorer(element_type)(item, exp, embedding=embedding)
    if element_type == "free_text":
        answer_tokens = set(_tokenize(str(exp)))
        item_tokens = set(_tokenize(str(item)))
        if answer_tokens:
            coverage = len(answer_tokens & item_tokens) / len(answer_tokens)
            if coverage >= get_verification_coverage():
                return max(float(base), coverage)
    return base


def score_entity_list(element_type: str, pred, exp, embedding=None,
                      partial_gt: bool = False) -> EntityListScore:
    """Pairwise similarity matrix + Hungarian assignment (scipy), thresholded,
    then precision/recall/F1 over the matched set.

    ``partial_gt=True`` (label sets that are QA-answer samples rather than
    exhaustive lists): role-word label items count as matched whenever the
    prediction names any party, and the reported score becomes ground-truth
    coverage (recall)."""
    pred_items = [str(item) for item in _as_list(pred)]
    exp_items = [str(item) for item in _as_list(exp)]
    if not pred_items and not exp_items:
        return EntityListScore("", 1.0, 1.0, 1.0, 0, 0, 0, partial_gt)
    if not pred_items:
        return EntityListScore("", 0.0, 0.0, 0.0, 0, 0, len(exp_items), partial_gt)
    if not exp_items:
        return EntityListScore("", 1.0, 0.0, 0.0, 0, len(pred_items), 0, partial_gt)

    scorer = _element_scorer(element_type)
    threshold = get_bipartite_match_threshold()
    n_pred, n_exp = len(pred_items), len(exp_items)

    if partial_gt:
        pred_token_lists = [_tokenize(str(p)) for p in pred_items]

        def _label_contained(label: str) -> bool:
            """Check whether a three-to-six-token label occurs contiguously in a prediction."""
            label_tokens = _tokenize(str(label))
            if not label_tokens:
                return False
            n = len(label_tokens)
            if n < 3 or n > 6:
                return False
            return any(
                label_tokens == tokens[i:i + n]
                for tokens in pred_token_lists
                for i in range(len(tokens) - n + 1)
            )

        real_exp = [e for e in exp_items if not (_is_role_word(e) or _label_contained(e))]
        role_items = sum(1 for e in exp_items if _is_role_word(e))
        contained_items = n_exp - len(real_exp) - role_items
        pred_has_party = any(not _is_role_word(p) for p in pred_items)
    else:
        real_exp = exp_items
        role_items = 0
        contained_items = 0
        pred_has_party = False

    # Skip the Hungarian machinery for trivially scored single-element lists.
    if len(pred_items) == 1 and len(real_exp) == 1:
        matched = 1 if _element_similarity(
            element_type, pred_items[0], real_exp[0], embedding
        ) >= threshold else 0
    else:
        try:
            import numpy as np
            from scipy.optimize import linear_sum_assignment

            sim = np.array([
                [_element_similarity(element_type, p, e, embedding) for e in real_exp]
                for p in pred_items
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
                for ei, e_item in enumerate(real_exp):
                    if ei in assigned_exp:
                        continue
                    s = _element_similarity(element_type, p_item, e_item, embedding)
                    if s >= best:
                        best, best_e = s, ei
                if best_e is not None:
                    matched += 1
                    assigned_exp.add(best_e)

    if role_items and pred_has_party:
        matched += role_items
    if contained_items:
        matched += contained_items

    # Role-word/contained credits are a BOUNDED credit, not an unbounded
    # additive bonus: nothing tracks which distinct predicted items were
    # consumed, so without the clamp an all-role-word expected list vs a
    # single named party yields precision 2/1 = 2.0 (hub#38). The composite
    # may never exceed the smaller of the two list sizes.
    matched = min(matched, n_pred, n_exp)

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
        partial_gt=partial_gt,
    )


# ---------------------------------------------------------------------------
# Factuality audit — "everything the model reports must be TRUE"
# ---------------------------------------------------------------------------


def verify_list_items(items: list, doc_text: str, token_coverage: float | None = None) -> list[bool]:
    """Groundedness flags: is each item's content present in ``doc_text``?
    ``token_coverage`` is the minimum share of the item's normalized tokens
    found in the document (default from config)."""
    threshold = get_verification_coverage() if token_coverage is None else token_coverage
    dt_tokens = _containment_tokens(str(doc_text or ""))
    flags: list[bool] = []
    for item in items or []:
        it_tokens = _containment_tokens(str(item))
        if not it_tokens or not dt_tokens:
            flags.append(False)
            continue
        coverage = len(it_tokens & dt_tokens) / len(it_tokens)
        flags.append(coverage >= threshold)
    return flags


def audit_list_field(
    element_type: str,
    pred_items: list,
    exp_items: list,
    doc_text: str | None,
    token_coverage: float | None = None,
) -> dict:
    """Per-item truth audit for one list field: each predicted item is TRUE
    when it matches any ground-truth label OR its content is grounded in the
    source document. Returns counts plus verified_precision/hallucination_rate."""
    pred_items = [str(item) for item in _as_list(pred_items)]
    exp_items = [str(item) for item in _as_list(exp_items)]
    n_pred = len(pred_items)
    if n_pred == 0:
        return {"n_predicted": 0, "matched_gt": 0, "verified_in_doc": 0,
                "true_items": 0, "verified_precision": 0.0,
                "hallucinated": 0, "hallucination_rate": 0.0,
                "doc_verification": False}

    match_threshold = get_bipartite_match_threshold()
    dt_tokens = _containment_tokens(str(doc_text or ""))
    verify_threshold = get_verification_coverage() if token_coverage is None else token_coverage

    matched_gt = verified = true_items = 0
    for item in pred_items:
        hit_gt = any(
            _element_similarity(element_type, item, e) >= match_threshold
            for e in exp_items
        ) if exp_items else False
        it_tokens = _containment_tokens(item)
        in_doc = bool(it_tokens) and bool(dt_tokens) and (
            len(it_tokens & dt_tokens) / len(it_tokens) >= verify_threshold
        )
        if hit_gt:
            matched_gt += 1
        if in_doc:
            verified += 1
        if hit_gt or in_doc:
            true_items += 1

    return {
        "n_predicted": n_pred,
        "matched_gt": matched_gt,
        "verified_in_doc": verified,
        "true_items": true_items,
        "verified_precision": round(true_items / n_pred, 4),
        "hallucinated": n_pred - true_items,
        "hallucination_rate": round((n_pred - true_items) / n_pred, 4),
        "doc_verification": bool(doc_text),
    }


def _verification_tokens(value, field_type: str) -> set[str]:
    """Token set used to check a predicted VALUE against the source document.
    Dates are converted to prose BEFORE tokenizing so an ISO prediction
    matches the document's prose date."""
    text = str(value or "")
    if field_type == "date":
        parsed = _parse_date(text)
        if parsed is not None:
            text = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    return _containment_tokens(text)


# Date-like spans in a document: numeric (04-01-06, 3/24/2006), month-first
# prose (April 1, 2006), or day-first prose ("18th day of August, 2014",
# incl. OCR artifacts like "18t h day of").
_DATE_CANDIDATE_RE = re.compile(
    r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}[a-z]*(?:\s+[a-z]+)*\s+day\s+of\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*,?\s*\d{4}\b",
    flags=re.IGNORECASE,
)


def _date_grounded_in_doc(pred_date, doc_text: str) -> bool:
    """True when the document contains a date parseable to ``pred_date``,
    in ANY format (numeric dashed, slashed, or prose month-name)."""
    if pred_date is None or not doc_text:
        return False
    for match in _DATE_CANDIDATE_RE.finditer(doc_text):
        candidate = _parse_date(match.group(0))
        if candidate is not None and candidate == pred_date:
            return True
    return False


def audit_scalar_field(field_type: str, pred, exp, doc_text: str | None) -> dict:
    """Factuality audit for one SCALAR (non-list) field.

    The predicted value is TRUE when it matches the ground truth (element
    scorer above the match threshold) OR its content is grounded in the
    source document. Fields the model left empty are not audited."""
    pred = str(pred or "")
    if not pred.strip():
        return {"n_predicted": 0, "matched_gt": 0, "verified_in_doc": 0,
                "true_items": 0, "verified_precision": 0.0,
                "hallucinated": 0, "hallucination_rate": 0.0,
                "doc_verification": bool(doc_text)}
    scorer = FIELD_SCORERS.get(field_type, score_name_field)
    threshold = get_bipartite_match_threshold()
    matched = exp not in (None, "") and scorer(pred, str(exp)) >= threshold
    in_doc = False
    if doc_text:
        if field_type == "date":
            in_doc = _date_grounded_in_doc(_parse_date(pred), doc_text)
        else:
            it_tokens = _verification_tokens(pred, field_type)
            dt_tokens = _containment_tokens(doc_text)
            in_doc = bool(it_tokens) and bool(dt_tokens) and (
                len(it_tokens & dt_tokens) / len(it_tokens) >= get_verification_coverage()
            )
    true = bool(matched) or bool(in_doc)
    return {
        "n_predicted": 1,
        "matched_gt": int(bool(matched)),
        "verified_in_doc": int(bool(in_doc)),
        "true_items": int(true),
        "verified_precision": 1.0 if true else 0.0,
        "hallucinated": 0 if true else 1,
        "hallucination_rate": 0.0 if true else 1.0,
        "doc_verification": bool(doc_text),
    }


def _split_clause_spans(text: str) -> list[str]:
    """Split one predicted obligation string into discrete clause fragments.

    Contracts quote several distinct clauses inside a single array item,
    separated by newlines, semicolons, or sentence-final punctuation. Splitting
    there keeps each operative clause a self-contained span so the contained-
    label rule can fire on its own tokens instead of being diluted by the
    other clauses in the merged item (issue #21). Guards against splitting on
    decimals ("$5.00"), section refs ("1.2"), and abbreviations ("U.S.")."""
    parts = re.split(
        r"(?:\s*\n\s*|\s*;\s+|(?<=[A-Za-z)][.!?])\s+(?=[A-Z0-9\"(]))",
        str(text or ""),
    )
    return [p.strip() for p in parts if p.strip()]


def disaggregate_clause_spans(items, min_span_tokens: int = 4) -> list[str]:
    """Disaggregate multi-clause predicted list items into discrete,
    sentence-level spans for scoring.

    Each input item is split into clause fragments; fragments shorter than
    ``min_span_tokens`` are merged back into the preceding fragment so a short
    standalone clause is not atomized. Returns the flat, de-duplicated list of
    spans. When an item is already a single clause it passes through intact.
    """
    out: list[str] = []
    for raw in _as_list(items):
        merged: list[str] = []
        for piece in _split_clause_spans(str(raw)):
            if merged and len(_tokenize(piece)) < min_span_tokens:
                merged[-1] = f"{merged[-1]} {piece}".strip()
            else:
                merged.append(piece)
        out.extend(p for p in merged if p.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for span in out:
        key = " ".join(_tokenize(span))
        if key not in seen:
            seen.add(key)
            unique.append(span)
    return unique


def _presence_candidates(predicted: dict, category: str, field: str) -> list[str]:
    """Candidate predicted spans for one CUAD YES/NO category.

    Prefers spans routed explicitly by the extractor's reasoning trace
    (``reasoning.entries[]`` whose ``field`` is the canonical CUAD category
    name — issue #21 retag), falling back to the category's mapped field
    (e.g. ``cuad_clauses``): both the disaggregated spans and the raw items,
    so a model that quotes a multi-sentence clause as one item is not lost to
    sentence-level splitting. Disable routing with
    ``trace_knobs.reasoning_routes_presence``.
    """
    if get_settings().trace_knobs.reasoning_routes_presence:
        entries = (predicted.get("reasoning") or {}).get("entries") or []
        routed = [
            str(e.get("evidence") or e.get("section_ref") or "")
            for e in entries
            if str(e.get("field") or "").strip() == category
        ]
        if routed:
            return [r for r in routed if r.strip()]
    items = disaggregate_clause_spans(predicted.get(field))
    seen = set(items)
    for raw in _as_list(predicted.get(field)):
        text = str(raw)
        if text.strip() and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _presence_matched(item: str, answer: str) -> bool:
    """Whether one predicted span covers the category's labeled clause.

    Matched when the labeled clause text is token-contained in the span at the
    verification coverage (0.7), or the embedding rescue raises the semantic
    similarity at the presence embedding threshold (0.7) — issue #21 fix #3.
    """
    if not answer or not item:
        return False
    if score_containment_field(item, answer) >= get_verification_coverage():
        return True
    if not embedding_enabled():
        return False
    embedding = _get_embedding()
    if embedding is None:
        return False
    try:
        sim = embedding.similarity(item, answer)
    except Exception:
        logger.warning("presence_embedding_similarity_failed", exc_info=True)
        return False
    return sim is not None and float(sim) >= get_presence_embedding_threshold()


def score_category_presence(predicted: dict | None, presence_expectations: dict,
                            field_types: dict[str, str]) -> tuple[float, dict]:
    """Binary YES/NO presence scoring for CUAD's presence-type categories.

    ``presence_expectations``: ``{category: {"expected": bool, "answer": str,
    "field": str}}``. Returns ``(score, detail)`` where ``score`` is the share
    of expected-True categories whose clause text is matched by a predicted
    span. A category is matched when ANY candidate span — the disaggregated
    items of the category's mapped field, or the reasoning-trace entry tagged
    with the canonical category name — covers the labeled clause by token
    containment or embedding similarity at the 0.7 threshold (issue #21)."""
    predicted = predicted or {}
    matched = 0
    expected_true = 0
    detail: dict[str, dict] = {}
    for category, expectation in sorted((presence_expectations or {}).items()):
        field = expectation.get("field") or "cuad_clauses"
        if not expectation.get("expected"):
            detail[category] = {"expected": False, "matched": None, "field": field}
            continue
        expected_true += 1
        answer = str(expectation.get("answer") or "")
        items = _presence_candidates(predicted, category, field)
        hit = any(_presence_matched(item, answer) for item in items)
        if hit:
            matched += 1
        detail[category] = {"expected": True, "matched": hit, "field": field,
                            "answer": answer, "predicted_items": len(items)}
    score = round(matched / expected_true, 4) if expected_true else 1.0
    return score, detail


# ---------------------------------------------------------------------------
# Embedding second signal (local sentence-transformers, remote OpenRouter
# embeddings fallback, lazy)
# ---------------------------------------------------------------------------

_REMOTE_EMBEDDING_MODEL = "openai/text-embedding-3-small"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class _EmbeddingMatcher:
    """Lazy singleton embedding cache. similarity() returns None whenever no
    embedder is available — callers then keep the string-only score."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "_EmbeddingMatcher":
        """Return the singleton matcher, creating it under the class lock."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        """Initialize unloaded local and remote backends and an empty vector cache."""
        self._model = None
        self._model_loaded = False
        self._client = None
        self._vectors: dict[str, object] = {}

    def _load_local(self) -> None:
        """Attempt local model loading once, retaining None when loading fails."""
        if self._model_loaded:
            return
        self._model_loaded = True
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(get_embedding_model())
        except Exception:
            self._model = None

    def _load_remote(self) -> None:
        """Initialize the remote embedding client when an API key is available."""
        if self._client is not None:
            return
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=os.environ.get("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL),
                api_key=api_key, timeout=120,
            )
        except Exception:
            self._client = None

    def _embed(self, text: str):
        """Produce a unit-normalized local or remote embedding, or None without a backend."""
        import numpy as np

        if self._model is not None:
            vector = self._model.encode([text], normalize_embeddings=True)[0]
        else:
            self._load_remote()
            if self._client is None:
                return None
            resp = self._client.embeddings.create(
                model=_REMOTE_EMBEDDING_MODEL, input=[text]
            )
            vector = np.asarray(resp.data[0].embedding, dtype=np.float64)
            norm = float(np.linalg.norm(vector))
            vector = vector / norm if norm else vector
        return vector

    def similarity(self, a: str, b: str) -> Optional[float]:
        """Return cached embedding cosine clipped to [0, 1], or None on unavailability."""
        if not embedding_enabled():
            return None
        try:
            self._load_local()  # idempotent; sets self._model if available
            import numpy as np

            va = self._vectors.get(a)
            if va is None:
                va = self._embed(a)
                if va is None:
                    return None
                self._vectors[a] = va
            vb = self._vectors.get(b)
            if vb is None:
                vb = self._embed(b)
                if vb is None:
                    return None
                self._vectors[b] = vb
            sim = float(np.dot(va, vb))
            return min(1.0, max(0.0, sim))
        except Exception:
            return None


def _get_embedding() -> Optional[_EmbeddingMatcher]:
    """Return the shared embedding matcher when enabled, or None on failure."""
    if not embedding_enabled():
        return None
    try:
        return _EmbeddingMatcher.get()
    except Exception:
        return None


def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed ``texts`` with the scorer's own model (unit-normalized vectors).

    The public handle on the embedding backend that ``similarity`` uses:
    the local SentenceTransformer named by ``get_embedding_model()`` when it
    loads, else the remote fallback. Vectors are cached per text for the
    process. Returns ``None`` when embeddings are disabled or no backend is
    available, or if any text fails to embed (fail-soft — callers skip the
    cosine signal rather than score with a partial batch).
    """
    matcher = _get_embedding()
    if matcher is None:
        return None
    try:
        matcher._load_local()
        out: list[list[float]] = []
        for text in texts:
            vector = matcher._vectors.get(text)
            if vector is None:
                vector = matcher._embed(text)
                if vector is None:
                    return None
                matcher._vectors[text] = vector
            out.append([float(x) for x in vector])
        return out
    except Exception:
        return None


_CMS_FACILITY_RE = re.compile(
    r"\bfacility\s+provider\s*(?:\b(?:id|identifier|number|no)\b)?[\s:#.-]*([a-z0-9]*\d[a-z0-9]*)"
)
_CMS_NPI_RE = re.compile(r"(?<![a-z0-9])(\d{10})(?![a-z0-9])")
_CMS_NDC_RE = re.compile(r"(?<![a-z0-9])(\d{9,11})(?![a-z0-9])")
_CMS_NDC_SEGMENTS_RE = re.compile(r"(?<![a-z0-9-])(\d{4,5})-(\d{3,4})-(\d{1,2})(?![a-z0-9-])")


def _cms_doc_signature(text) -> tuple[str, str]:
    """Classify one CMS supporting-document item as ``(kind, value)``.

    Identifier items compare as exact tokens: ``npi`` (10 digits), ``ndc``
    (11-digit 5-4-2 form: a hyphenated labelled NDC pads each segment, an
    unhyphenated one is left-padded) and
    ``facility_provider`` (alphanumeric id). Anything else is a descriptor
    (``HCPCS detail (1 codes)``) reduced to its normalized type label: letters
    only, count and ``code(s)`` words dropped.
    """
    raw = str(text or "").strip().lower()
    facility = _CMS_FACILITY_RE.search(raw)
    if facility:
        return "facility_provider", facility.group(1)
    if re.search(r"\bndc\b", raw):
        # A hyphenated 10-digit NDC (4-4-2, 5-3-2, 5-4-1) pads the short
        # segment, not the whole number, to reach the 11-digit 5-4-2 form.
        segmented = _CMS_NDC_SEGMENTS_RE.search(raw)
        if segmented:
            labeler, product, package = segmented.groups()
            return "ndc", labeler.zfill(5) + product.zfill(4) + package.zfill(2)
    squeezed = re.sub(r"(?<=\d)-(?=\d)", "", raw)
    if re.search(r"\bndc\b", squeezed):
        match = _CMS_NDC_RE.search(squeezed)
        if match:
            return "ndc", match.group(1).zfill(11)
    npi = _CMS_NPI_RE.search(squeezed)
    if npi:
        return "npi", npi.group(1)
    ndc = re.search(r"(?<![a-z0-9])(\d{11})(?![a-z0-9])", squeezed)
    if ndc:
        return "ndc", ndc.group(1)
    words = [w for w in re.sub(r"[^a-z]+", " ", raw).split() if w not in ("code", "codes")]
    return "descriptor", " ".join(words)


def score_cms_doc_ref(pred, exp, embedding=None) -> float:
    """Identifier-aware match of one CMS supporting-document item (1.0 or 0.0).

    Identifiers (NPI, NDC, facility provider id) must match as exact tokens of
    the same kind; descriptor items match by normalized type label, so
    ``HCPCS detail (2 codes)`` matches ``HCPCS detail (1 codes)``. Prose
    similarity never earns credit: a near-miss identifier is a different
    provider or drug.
    """
    pk, pv = _cms_doc_signature(pred)
    ek, ev = _cms_doc_signature(exp)
    if not pv or not ev:
        return 0.0
    return 1.0 if (pk, pv) == (ek, ev) else 0.0


# ---------------------------------------------------------------------------
# Dispatch + composite scoring
# ---------------------------------------------------------------------------

FIELD_SCORERS = {
    "cms_doc_ref": score_cms_doc_ref,
    "id": score_id_field,
    "date": score_date_field,
    "money": score_money_field,
    "name": score_name_field,
    "label": score_label_field,
    "free_text": score_free_text_field,
    "containment": score_containment_field,
}

# List-valued field types whose element scorer is specified as a suffix:
# "entity_list:name", "entity_list:free_text", ...
LIST_PREFIX = "entity_list"


def is_entity_list(field_type: str) -> bool:
    """Recognize the entity-list field type and its element-type suffix variants."""
    return field_type == LIST_PREFIX or field_type.startswith(LIST_PREFIX + ":")


def score_field(field_type: str, pred, exp, embedding=None, partial_gt: bool = False):
    """Score one field. Returns a float, or an EntityListScore for list fields."""
    if is_entity_list(field_type):
        element_type = field_type.split(":", 1)[1] if ":" in field_type else "name"
        return score_entity_list(element_type, pred, exp, embedding=embedding,
                                 partial_gt=partial_gt)
    scorer = FIELD_SCORERS.get(field_type, score_name_field)
    return scorer(pred, exp, embedding=embedding)


def _heuristic_field_type(field_name: str, value) -> str:
    """Fallback type inference for fields not mapped in the taxonomy."""
    if isinstance(value, list):
        return LIST_PREFIX
    name = field_name.lower()
    if "date" in name:
        return "date"
    if any(k in name for k in ("value", "amount", "fee", "compensation", "price",
                               "cost", "salary", "consideration", "total")):
        return "money"
    if any(k in name for k in ("number", "id", "docket", "reference", "filing")):
        return "id"
    return "name"


def get_field_types(doc_class: str, taxonomy: dict | None = None) -> dict[str, str]:
    """Field->scoring-type mapping for a doc class.

    Resolution order:
    1. Explicit ``taxonomy`` dict (``{"doc_classes": [{"key", "field_types"}]}``)
       when passed — the caller's own taxonomy wins.
    2. The wired taxonomy captured by ``configure_from_taxonomy()``
       (``Settings.doc_class_field_types``) when the consuming project wired
       once at import.
    3. ``{}`` when neither is available (no taxonomy configured).

    Returns {} when the class is absent. Extract aliases (currently none)
    resolve to their canonical class; ``merger_agreement`` is its own map.
    """
    from ..pipeline.mailroom import EXTRACT_CLASS_ALIASES

    resolved = EXTRACT_CLASS_ALIASES.get(doc_class, doc_class)
    if taxonomy:
        for cls in taxonomy.get("doc_classes", []):
            if cls.get("key") == doc_class or cls.get("key") == resolved:
                return dict(cls.get("field_types") or {})
        return {}
    wired = get_settings().doc_class_field_types
    return dict(wired.get(resolved) or wired.get(doc_class) or {})


@dataclass
class ExtractionScoreResult:
    """Per-document field scores, judge-review candidates, and list factuality audits."""
    doc_class: str
    field_scores: dict[str, float]
    overall_score: Optional[float]
    ambiguous_fields: list[str]
    entity_list_scores: dict[str, EntityListScore] = field(default_factory=dict)
    # Factuality audit per list field: {field: audit dict} with
    # verified_precision / hallucination_rate (see audit_list_field).
    entity_list_audit: dict[str, dict] = field(default_factory=dict)
    #: Captured confidence / reasoning knobs (never in ``field_scores``).
    trace: dict[str, Any] | None = None

    @property
    def needs_judge_review(self) -> bool:
        """Return whether any field score requires judge review."""
        return bool(self.ambiguous_fields)

    @property
    def audit_support(self) -> dict:
        """Canonical field-count factuality support (additive; serialization unchanged)."""
        from .field_support import field_audit_support

        return field_audit_support(self.entity_list_audit)

    @property
    def overall_verified_precision(self) -> Optional[float]:
        """Mean verified_precision across the row's scored list fields
        (None when no list field was audited)."""
        values = [a["verified_precision"] for a in self.entity_list_audit.values()
                  if a.get("n_predicted")]
        return round(sum(values) / len(values), 4) if values else None

    def to_dict(self) -> dict:
        """Serializable form (for manifests / experiment logs)."""
        return {
            "doc_class": self.doc_class,
            "field_scores": self.field_scores,
            "overall_score": self.overall_score,
            "ambiguous_fields": self.ambiguous_fields,
            "entity_list_scores": {
                k: v.to_dict() for k, v in self.entity_list_scores.items()
            },
            "entity_list_audit": self.entity_list_audit,
            "trace": self.trace,
        }


def score_extraction(
    doc_class: str,
    field_types: dict[str, str],
    predicted: dict | None,
    expected: dict | None,
    doc_text: str | None = None,
    *,
    audit_predicted: dict | None = None,
    cms_aware: bool = True,
) -> ExtractionScoreResult:
    """Score one extraction deterministically.

    ``cms_aware=False`` disables the CMS-row rules (#93) — for callers whose
    ``expected`` is not ground truth (the auditor compares two model outputs).

    - Only expected fields with a non-null/non-empty value count toward the
      overall score (null expectations are not requirements). Stringified
      JSON containers are parsed on both sides; empty containers and
      case-insensitive ``null`` / ``none`` / ``n/a`` / ``n.a.`` expectations
      are skipped. Trace and annotation keys are never scored; retired
      prompt keys require an explicit entry in ``field_types``.
    - ``overall_score`` is the mean of the per-field scores (None when no
      field is scored).
    - ``ambiguous_fields`` collects fields landing in the ambiguous band —
      the signal that escalates to the LLM judge.
    - List fields also produce ``entity_list_scores`` with precision/recall.
    - When verification is enabled and ``doc_text`` is nonempty, populated
      fields in ``field_types`` produce ``entity_list_audit`` entries,
      including scalar fields and fields absent from GT. Trace and
      annotation keys are excluded. ``audit_predicted``, when supplied,
      preserves predictions omitted from GT scoring for this audit.
    """
    had_prediction = bool(predicted)
    predicted = predicted or {}
    expected = expected or {}
    partial_gt_fields = get_partial_gt_fields()
    containment_fields = get_containment_fields()
    needs_embedding = any(
        ft in ("name", "free_text") or (is_entity_list(ft) and (ft.split(":", 1)[1] if ":" in ft else "name") in ("name", "free_text"))
        for ft in field_types.values()
    )
    embedding = _get_embedding() if needs_embedding else None

    field_scores: dict[str, float] = {}
    ambiguous: list[str] = []
    entity_list_scores: dict[str, EntityListScore] = {}
    entity_list_audit: dict[str, dict] = {}
    # CMS rows (expected carries a valid ``cms_source_table``) get the CMS-aware
    # fields: documented-absent adjuster / denial_reasons and identifier-aware
    # supporting_documents. Non-CMS rows keep the generic behaviour.
    cms_table = expected_cms_source_table(doc_class, expected) if cms_aware else None

    expected_items = list(expected.items())
    if cms_table is not None:
        # A CMS row whose GT omits a documented-absent key still penalises a
        # stated value, as the field-micro counts already do (a false
        # positive). Silence on an omitted key earns no credit: it is not
        # scored, so partial GT does not inflate the mean.
        expected_items += [
            (key, "") for key in CMS_DOCUMENTED_ABSENT_FIELDS
            if key not in expected
            and (not field_types or key in field_types)
            and not is_empty_value(parse_json_container(predicted.get(key)))
        ]

    for key, exp_value in expected_items:
        if key in NEVER_SCORED_FIELDS:
            continue
        if key in RETIRED_PROMPT_KEYS and key not in field_types:
            continue
        exp_value = parse_json_container(exp_value)
        if is_empty_value(exp_value):
            if cms_table is not None and key in CMS_DOCUMENTED_ABSENT_FIELDS:
                # Documented-absent on a CMS row: saying nothing is correct,
                # a stated value is a false positive. Never skipped.
                # No parsed prediction earns nothing (a null row must not
                # collect free credit for staying silent).
                if had_prediction:
                    absent_pred = parse_json_container(predicted.get(key))
                    if isinstance(absent_pred, (list, tuple)):
                        absent_pred = [i for i in absent_pred if not is_empty_value(i)]
                    field_scores[key] = 1.0 if is_empty_value(absent_pred) else 0.0
            continue
        field_type = (field_types.get(key)
                      or (CMS_ROW_FIELD_TYPES["cms_source_table"] if key == "cms_source_table"
                          else _heuristic_field_type(key, exp_value)))
        if cms_table is not None and key in CMS_ROW_FIELD_TYPES:
            field_type = CMS_ROW_FIELD_TYPES[key]
        if (doc_class == "insurance_claim" and key == "claim_type"
                and str(parse_json_container(predicted.get(key)) or "").strip().lower() in CMS_CLAIM_TYPES):
            # A CMS table token is never a product line (#93): exact label
            # scoring, so it earns no fuzzy credit even when the row was not
            # detected as CMS (no expected_subclass passed).
            field_type = "label"
        # Judge escalation uses the configured type's band, not the
        # containment override applied for scoring below.
        original_field_type = field_type
        pred_value = predicted.get(key)
        if pred_value is not None:
            pred_value = parse_json_container(pred_value)
        if field_type == "label" and doc_class:
            # Closed labels (0.28.0): fold aliases onto the scored token first, then
            # exact match. An unfoldable value is kept as written: published v9 gold
            # still carries off-vocabulary labels (urgency "low", intent "inform"),
            # and a prediction that repeats the gold verbatim matches it.
            exp_value = canonical_label(doc_class, key, exp_value) or exp_value
            if pred_value is not None:
                pred_value = canonical_label(doc_class, key, pred_value) or pred_value
        if doc_class == "insurance_claim" and key == "cms_source_table" and is_empty_value(pred_value):
            # Compatibility with the frozen insurance prompt, which reports the
            # table on ``claim_type``: when ``cms_source_table`` itself is blank
            # the table token is read from there for THIS field only. It earns
            # nothing on ``claim_type``, which scores the product line alone.
            token = str(predicted.get("claim_type") or "").strip().lower()
            pred_value = token if token in CMS_CLAIM_TYPES else None
        if pred_value is None:
            # A null answer satisfies a null-expectation date (blank-template
            # or label-only GT holds no real date).
            if (field_type == "date"
                    and _parse_date(str(exp_value)) is None
                    and _date_expected_is_null(str(exp_value))):
                score = 1.0
            else:
                score = 0.0
        else:
            if key in containment_fields and field_type in ("name", "free_text", "containment"):
                field_type = "containment"
            result = score_field(field_type, pred_value, exp_value, embedding=embedding,
                                 partial_gt=key in partial_gt_fields)
            if isinstance(result, EntityListScore):
                result.field_name = key
                entity_list_scores[key] = result
                score = result.score
            else:
                score = result
        score = round(score, 4)
        field_scores[key] = score
        if field_is_ambiguous(original_field_type, score):
            ambiguous.append(key)

    if verification_enabled() and doc_text:
        # Factuality audit for EVERY content field the model populated —
        # including fields the ground truth does not label.
        audit_source = predicted if audit_predicted is None else audit_predicted
        for key, field_type in sorted(field_types.items()):
            pred_value = parse_json_container(audit_source.get(key))
            if is_empty_value(pred_value) or key in entity_list_audit:
                continue
            if key in NEVER_SCORED_FIELDS:
                continue
            if is_entity_list(field_type):
                element_type = field_type.split(":", 1)[1] if ":" in field_type else "name"
                entity_list_audit[key] = audit_list_field(
                    element_type, pred_value, expected.get(key) or [], doc_text,
                )
            else:
                entity_list_audit[key] = audit_scalar_field(
                    field_type, pred_value, expected.get(key), doc_text,
                )

    scored = list(field_scores.values())
    overall = round(sum(scored) / len(scored), 4) if scored else None
    from .trace_knobs import capture_trace_knobs

    return ExtractionScoreResult(
        doc_class=doc_class,
        field_scores=field_scores,
        overall_score=overall,
        ambiguous_fields=ambiguous,
        entity_list_scores=entity_list_scores,
        entity_list_audit=entity_list_audit,
        trace=capture_trace_knobs(predicted, expected=expected, correctness=overall),
    )
