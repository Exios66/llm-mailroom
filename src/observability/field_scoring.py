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


def get_partial_gt_fields() -> set[str]:
    """Field names whose ground truth is a PARTIAL sample of the document's
    content (QA-answer snippets), not an exhaustive list.

    For these fields the model's extraction is usually MORE complete than the
    label set, so precision penalizes correctness. They are scored by
    GROUND-TRUTH COVERAGE (recall over matched label items) instead of F1.
    """
    try:
        return set(_field_scoring_config().get("partial_gt_fields") or [])
    except Exception:
        return set()


def get_containment_fields() -> set[str]:
    """Field names scored by EXPECTED-WITHIN-PREDICTED token containment.

    For verbatim-clause fields whose ground truth is one sentence of a longer
    passage, a model that returns a SUPERSET (expected sentence plus its
    riders) is correct on the substance. Containment scores the share of the
    expected text's normalized tokens covered by the prediction, so
    over-inclusion no longer zeroes the field.
    """
    try:
        return set(_field_scoring_config().get("containment_fields") or [])
    except Exception:
        return set()


def verification_enabled() -> bool:
    """Whether predicted list items are verified against the source document
    text (the factuality guard)."""
    cfg = _field_scoring_config().get("factuality_verification") or {}
    return bool(cfg.get("enabled", True))


def get_verification_coverage() -> float:
    """Minimum normalized-token coverage of a predicted item within the
    source document text for it to count as grounded (not hallucinated)."""
    cfg = _field_scoring_config().get("factuality_verification") or {}
    try:
        return float(cfg.get("token_coverage", 0.7))
    except (TypeError, ValueError):
        return 0.7


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
    """Parse to a canonical datetime.date, or None when unparseable.

    Handles the forms that appear in ground truth: ISO, mm/dd/yyyy,
    "March 3, 2024", ordinal prose ("10th day of January 2000"), and
    stray trailing artifacts like "1st day of April, 2007 (".
    """
    if not isinstance(text, str):
        return None
    s = _WS_RE.sub(" ", text.strip())
    if not s:
        return None
    # "10th day of January 2000" -> "10 day of January 2000"; dateutil then
    # still rejects it, so normalize the whole "N day of MONTH YEAR" pattern.
    s = _ORDINAL_RE.sub(r"\1", s)
    # OCR/scan artifacts can break the day-of pattern ("18t h day of August
    # 2014" -> "18 day of August 2014"): drop stray letters between the day
    # number and "day of".
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
    # Drop stray trailing punctuation/artifacts ("2007 (", "2007,", ...).
    s = re.sub(r"[(\[\{,]+$", "", s).strip()
    try:
        from dateutil import parser

        dt = parser.parse(s)
        return dt.date()
    except (ValueError, OverflowError):
        return None


def parse_date(text) -> "object | None":
    """Public alias of :func:`_parse_date` — canonical datetime.date for any
    date string, or None when unparseable. Used by the MAE diagnostics."""
    return _parse_date(text)


def parse_money(text) -> "float | None":
    """Public alias of :func:`_parse_money` — canonical USD float for any
    money-style string, or None when unparseable. Used by MAE diagnostics."""
    return _parse_money(text)


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
    """Use embedding cosine as a second signal only when the string score is
    ambiguous (below the rescue threshold) — never overriding a confident
    string-level reject. Fetches the matcher lazily when not provided."""
    if string_score >= get_embedding_rescue_below():
        return string_score
    # Empty predictions/labels are never "rescued" by embeddings — a blank
    # answer must stay a miss, not pick up cosine noise (~0.4-0.5 vs any text).
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
    """Normalized fuzzy matching: max of Jaro-Winkler and token-set ratio,
    with embedding rescue for lexically distant but semantically equal names.

    Jaro-Winkler alone is dangerously lenient when the token sets are
    DISJOINT (short-vs-long names: "BETA" vs "SOVEREIGN STATE BANK OF OHIO"
    scores ~0.62), so the raw JW score is only trusted when the two names
    share at least one token. Disjoint names fall back to the token-set
    ratio, which stays low for genuinely different entities.

    Token containment runs first: a short title or name whose EVERY token
    appears in the prediction ("FRANCHISE AGREEMENT" inside "Goosehead
    Insurance Agency, LLC Franchise Agreement") is contained — 1.0.
    """
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


def score_free_text_field(pred, exp, embedding=None) -> float:
    """SQuAD-style token F1, with embedding rescue for paraphrases."""
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
    f1 = _token_f1(pred, exp)
    return _with_embedding_rescue(f1, pred, exp, embedding)


def score_containment_field(pred, exp, embedding=None) -> float:
    """Share of the EXPECTED text's normalized tokens covered by the prediction.

    Suited to verbatim-clause fields where the ground truth is one sentence of
    a longer passage and the model legitimately returns a superset (the
    expected sentence plus its riders or citations). A prediction fully
    containing the expected text scores 1.0; truncating the expected text
    scores below 1.0 (missing tokens lower the coverage).
    """
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


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]


def _date_expected_is_null(exp: str) -> bool:
    """True when the expected date text carries no real date: a blank
    template line ("_____ day of ________, 19____", "____ day of May, 2000")
    or a bare label ("Effective Date:", "the date first written above").
    Blank templates are detected by underscores; labels by the absence of
    any date evidence (no month name, no four-digit year).
    """
    if "_" in exp or "blank" in exp.lower():
        return True
    low = exp.lower()
    if not any(m in low for m in _MONTH_NAMES):
        if not re.search(r"\b(19|20)\d{2}\b", low):
            return True
    return False


def _date_phrase_contained(container: str, target: str) -> bool:
    """True when a date phrase stated in ``target`` appears in ``container``.

    Checks the target's raw normalized phrase plus canonical renderings of
    its parsed components (ISO, ``Month D YYYY``, ``D Month YYYY``), so a
    prose label quoted as ISO (or vice versa) still counts as contained.
    """
    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text).lower())).strip()

    nc = norm(container)
    if not nc:
        return False
    target_norm = norm(target)
    if not target_norm:
        return False
    # A contained phrase only counts when it states a real date (month or
    # day level): a bare year ("2024" — which dateutil would parse) must not
    # earn full credit, and a 3-token phrase like "november 5 1996" always
    # states a full date.
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
        f"{month} {dt.day} {dt.year}",
    ]
    return any(norm(r) in nc for r in renderings)


def score_date_field(pred, exp, embedding=None) -> float:
    """Parse to canonical date, then exact match — with containment and
    partial-credit fallbacks.

    Rules, in order:

    1. Null-expectation rule: a blank-template/label-only expected date holds
       NO real date — a null prediction is CORRECT (1.0), a fabricated date
       is wrong (0.0). Only fires when the expected is genuinely
       template/label garbage: a PARSEABLE expected date is never null.
    2. Containment -> 1.0: the labeler's date phrase appears inside the
       prediction (or the predicted phrase inside the label) — the model
       quoted the field's date among several.
    3. Exact parsed-date equality -> 1.0.
    4. Partial credit by shared components when both parse: year+month ->
       0.67, within a 45-day cluster (execution vs defined effective date) ->
       0.67, year-only -> 0.33.
    5. Unparseable values fall back to fuzzy string matching (unchanged).
    """
    if not isinstance(pred, str):
        pred = str(pred or "")
    if not isinstance(exp, str):
        exp = str(exp or "")
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
    partial_gt: bool = False

    @property
    def score(self) -> float:
        """Composite score: F1 normally; GROUND-TRUTH COVERAGE (recall) when
        the field's label set is a partial sample (partial_gt_fields)."""
        return self.recall if self.partial_gt else self.f1


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _element_scorer(element_type: str):
    """Scalar scorer for entity-list elements (entity_list:<element_type>)."""
    return FIELD_SCORERS.get(element_type, score_name_field)


# Party ROLE labels that ground truth uses INSTEAD of the actual entity name
# ("Shipper.", "Sponsor", "Seller", "Producer", ...). The model extracts the
# real named party; a role word in the label set is matched whenever the
# prediction names at least one party (the role is instantiated).
_ROLE_WORDS = {
    "shipper", "sponsor", "seller", "buyer", "company", "producer", "reseller",
    "licensee", "licensor", "party", "parties", "transporter", "customer",
    "client", "vendor", "purchaser", "lender", "borrower", "guarantor",
    "manufacturer", "distributor", "owner", "operator",
}


def _is_role_word(text: str) -> bool:
    tokens = _tokenize(str(text))
    return 1 <= len(tokens) <= 3 and all(t in _ROLE_WORDS for t in tokens)


def _element_similarity(element_type: str, item: str, exp: str, embedding=None) -> float:
    """Similarity of one predicted list item against one ground-truth item.

    For ``free_text`` elements, ground-truth answers are often FRAGMENTS of
    the document's verbatim clause while the model extracts the FULL clause.
    Token-F1 alone under-scores long verbatim items, so when the item covers
    >= the verification coverage of the answer's plain tokens, that coverage
    is the similarity (the item IS the labeled clause).
    """
    base = _element_scorer(element_type)(item, exp, embedding=embedding)
    if element_type == "free_text":
        answer_tokens = set(_tokenize(str(exp)))
        item_tokens = set(_tokenize(str(item)))
        if answer_tokens:
            coverage = len(answer_tokens & item_tokens) / len(answer_tokens)
            if coverage >= get_verification_coverage():
                return max(float(base), coverage)
    return base


def score_entity_list(
    element_type: str, pred, exp, embedding=None, partial_gt: bool = False
) -> EntityListScore:
    """Pairwise similarity matrix + Hungarian assignment (scipy), thresholded,
    then precision/recall/F1 over the matched set.

    ``partial_gt=True`` (label sets that are QA-answer samples rather than
    exhaustive lists): role-word label items count as matched whenever the
    prediction names any party, and the reported score becomes ground-truth
    coverage (recall) — extra correct predictions no longer cut the score.
    Raw precision/recall/F1 are still computed for audit.
    """
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

    # Role-word labels are matched by the mere presence of named parties.
    # Beyond the role list, a label whose tokens appear verbatim inside a
    # predicted item is instantiated (the label is then a fragment of the
    # extracted name — "Consultant" inside 'Timothy Cabrera ("Consultant")',
    # the pronoun alias '"we," "us," or "our"' inside 'Goosehead Insurance
    # Agency, LLC ("we," "us," or "our")').
    if partial_gt:
        pred_token_lists = [_tokenize(str(p)) for p in pred_items]

        def _label_contained(label: str) -> bool:
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

    # Skip the Hungarian machinery when a single-element list is trivially
    # scored — cheaper and identical for the common one-name lists.
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
#
# Coverage scoring treats a GT hit as correct even when the model extracts
# MORE than the label set — but correctness must not mean "extra garbage".
# For every predicted list item we check: (a) does it match a ground-truth
# label, or (b) is its content actually present in the source document text
# (normalized token coverage)? Items that are NEITHER are counted as
# HALLUCINATED. This yields:
#   verified_precision = (GT-matched + doc-grounded) / n_predicted
#   hallucination_rate  = ungrounded / n_predicted


def verify_list_items(items: list, doc_text: str, token_coverage: float | None = None) -> list[bool]:
    """Groundedness flags: is each item's content present in ``doc_text``?

    Items are verbatim clause quotes or legal names; a genuine quote shares
    nearly all of its (stopword-filtered) tokens with the source document,
    while fabricated content shares few. ``token_coverage`` is the minimum
    share of the item's normalized tokens found in the document (default from
    config). Items with no content tokens are unverifiable (False).
    """
    threshold = get_verification_coverage() if token_coverage is None else token_coverage
    dt_tokens = _containment_tokens(str(doc_text or ""))
    flags: list[bool] = []
    for item in items or []:
        it_tokens = _containment_tokens(str(item))
        if not it_tokens:
            flags.append(False)
            continue
        if not dt_tokens:
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
    """Per-item truth audit for one list field.

    Each predicted item is TRUE when it matches any ground-truth label
    (element scorer above the match threshold) OR its content is grounded in
    the source document. Returns the counts plus the two trackers.
    """
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

    Dates are converted to prose ("November 1, 2002", unpadded day) BEFORE
    tokenizing so an ISO prediction ("2002-11-01") matches the document's
    prose date instead of tripping the containment check on format
    differences.
    """
    text = str(value or "")
    if field_type == "date":
        parsed = _parse_date(text)
        if parsed is not None:
            text = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    return _containment_tokens(text)


# Date-like spans in a document: numeric (04-01-06, 3/24/2006), month-first
# prose (April 1, 2006), or day-first prose ("18th day of August, 2014",
# incl. OCR artifacts like "18t h day of"). Used to ground predicted dates
# against the source text.
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
    source document. Dates are grounded by parsing the document's date
    candidates (any format); other values by normalized token coverage of
    the predicted value within the document text. Fields the model left
    empty are not reported content and are not audited (callers skip them).
    """
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


def score_category_presence(predicted: dict | None, presence_expectations: dict,
                            field_types: dict[str, str]) -> tuple[float, dict]:
    """Binary YES/NO presence scoring for presence-type categories.

    Each presence expectation answers "Yes" when a labeled clause exists (the
    extraction must cover that clause) and "No" when no clause exists
    (satisfied by default — fabricating such a clause is already punished by
    the factuality guard).

    ``presence_expectations``: ``{category: {"expected": bool, "answer": str,
    "field": str}}``.

    Returns ``(score, detail)`` where ``score`` is the share of expected-True
    categories whose clause text is matched by a predicted item in the
    category's mapped field, and ``detail`` records every category's outcome.
    """
    predicted = predicted or {}
    matched = 0
    expected_true = 0
    detail: dict[str, dict] = {}
    for category, expectation in sorted((presence_expectations or {}).items()):
        field = expectation.get("field") or "key_obligations"
        if not expectation.get("expected"):
            detail[category] = {"expected": False, "matched": None, "field": field}
            continue
        expected_true += 1
        answer = str(expectation.get("answer") or "")
        items = [str(item) for item in _as_list(predicted.get(field))]
        field_type = field_types.get(field) or "name"
        element_type = field_type.split(":", 1)[1] if is_entity_list(field_type) else field_type
        threshold = get_bipartite_match_threshold()
        answer_tokens = set(_tokenize(answer))
        hit = False
        for item in items:
            if _element_similarity(element_type, item, answer) >= threshold:
                hit = True
                break
            # The labeled clause text may be a fragment of the extracted item:
            # a covered category when >= 70% of the answer's plain tokens
            # appear within the item (e.g. "shall not assign" inside the full
            # verbatim anti-assignment clause).
            if answer_tokens:
                item_tokens = set(_tokenize(item))
                coverage = len(answer_tokens & item_tokens) / len(answer_tokens)
                if coverage >= get_verification_coverage():
                    hit = True
                    break
        if hit:
            matched += 1
        detail[category] = {"expected": True, "matched": hit, "field": field,
                            "answer": answer, "predicted_items": len(items)}
    score = round(matched / expected_true, 4) if expected_true else 1.0
    return score, detail


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


def warm_embedding_model(blocking: bool = False) -> None:
    """Load the embedding model OFF the document path (O-10).

    The first grounded run that needs a name/free-text embedding used to
    trigger a synchronous multi-minute SentenceTransformer download inside run
    finalization — hanging the router thread behind the run deadline. This
    kicks the load into a background thread at process start instead.

    Failures are logged (previously silent — ``similarity`` swallowed every
    exception and returned None); scoring simply keeps the string-only score.
    """
    import threading

    if not embedding_enabled():
        return

    def _run():
        try:
            matcher = _EmbeddingMatcher.get()
            matcher._load()
            logger.info("embedding_model_loaded", model=get_embedding_model())
        except Exception:
            logger.warning("embedding_model_load_failed",
                           model=get_embedding_model(), exc_info=True)

    if blocking:
        _run()
    else:
        threading.Thread(target=_run, name="embedding-model-warmup", daemon=True).start()


def _get_embedding() -> "_EmbeddingMatcher | None":
    if not embedding_enabled():
        return None
    try:
        return _EmbeddingMatcher.get()
    except Exception:
        logger.warning("embedding_matcher_unavailable", exc_info=True)
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
    "containment": score_containment_field,
}

# List-valued field types whose element scorer is specified as a suffix:
# "entity_list:name", "entity_list:free_text", "entity_list:id", ...
LIST_PREFIX = "entity_list"


def is_entity_list(field_type: str) -> bool:
    return field_type == LIST_PREFIX or field_type.startswith(LIST_PREFIX + ":")


def score_field(field_type: str, pred, exp, embedding=None, partial_gt: bool = False):
    """Score one field. Returns a float, or an EntityListScore for list fields."""
    if is_entity_list(field_type):
        element_type = field_type.split(":", 1)[1] if ":" in field_type else "name"
        return score_entity_list(
            element_type, pred, exp, embedding=embedding, partial_gt=partial_gt
        )
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
    # Factuality audit per list field: {field: audit dict} with
    # verified_precision / hallucination_rate (see audit_list_field).
    entity_list_audit: dict[str, dict] = field(default_factory=dict)

    @property
    def needs_judge_review(self) -> bool:
        return bool(self.ambiguous_fields)

    @property
    def overall_verified_precision(self) -> float | None:
        """Mean verified_precision across the row's audited content fields
        (None when no content field was audited)."""
        values = [a["verified_precision"] for a in self.entity_list_audit.values()
                  if a.get("n_predicted")]
        return round(sum(values) / len(values), 4) if values else None


def score_extraction(
    doc_class: str,
    field_types: dict[str, str],
    predicted: dict | None,
    expected: dict | None,
    doc_text: str | None = None,
) -> ExtractionScoreResult:
    """Score one extraction deterministically.

    - Only expected fields with a non-null/non-empty value count toward the
      overall score (null expectations are not requirements — except the
      date null-expectation rule, where a null answer to a template/label-only
      expected date is a full-credit answer).
    - ``containment_fields`` (governing_law/term_length/renewal_terms) are
      scored by EXPECTED-WITHIN-PREDICTED token containment; list fields in
      ``partial_gt_fields`` are scored by ground-truth coverage (recall).
    - ``overall_score`` is the mean of the per-field scores (None when no
      field is scored).
    - ``ambiguous_fields`` collects fields landing in the ambiguous band
      (``field_scoring.ambiguous_band`` or the per-type ``type_bands``) — the
      signal that escalates to the LLM judge.
    - List fields also produce ``entity_list_scores`` with precision/recall.
    - When ``doc_text`` is provided, EVERY content field the model populated
      (list items and scalar values alike, whether or not the ground truth
      labels that field) produces an ``entity_list_audit`` entry: a reported
      value is TRUE when it matches a ground-truth label OR its content is
      grounded in the source document (the factuality guard — nothing the
      model reports may be fabricated). ``overall_verified_precision`` is the
      mean over every audited field, so it is anchored to exactly the
      components that make it up.
    """
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

    for key, exp_value in expected.items():
        if exp_value is None or exp_value == "":
            continue
        field_type = field_types.get(key) or _heuristic_field_type(key, exp_value)
        pred_value = predicted.get(key)
        if pred_value is None:
            # A null answer satisfies a null-expectation date (blank-template
            # or label-only GT holds no real date — the model is right not to
            # fabricate one). Parseable expected dates are never null.
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
        if field_is_ambiguous(field_type, score):
            ambiguous.append(key)

    if verification_enabled() and doc_text:
        # Factuality audit for EVERY content field the model populated —
        # including fields the ground truth does not label. Only schema
        # content fields (``field_types``) are audited: meta fields like
        # ``confidence``/``reasoning`` are not reported content and never
        # audited.
        for key, field_type in sorted(field_types.items()):
            pred_value = predicted.get(key)
            if pred_value in (None, "", []) or key in entity_list_audit:
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
    return ExtractionScoreResult(
        doc_class=doc_class,
        field_scores=field_scores,
        overall_score=overall,
        ambiguous_fields=ambiguous,
        entity_list_scores=entity_list_scores,
        entity_list_audit=entity_list_audit,
    )
