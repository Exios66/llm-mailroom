"""Enron topic/sentiment and MAUD per-question extraction scorers.

These are the type-specific content evaluators that sit *alongside* typed
extraction (sender/recipient/…, ContractExtraction fields). They consume
corpus differentiators, not specialist extraction-schema fields:

- correspondence: ``content_topic`` (11 Enron topics) and ``sentiment_label``
  (negative / neutral / positive)
- merger_agreement: ``maud_clause_labels`` (22 Hub question keys → answer +
  category + ``valid_classes``), or the specialist ``maud_clauses``
  ``'<Question>: <Answer>'`` list

MAUD answers are validated against the question's own class catalog — the
GT record's ``valid_classes`` when present, otherwise the corpus union in
:mod:`llm_dojo_scoring.maud` — never "any non-empty text is valid".

All functions are deterministic pure functions over ``(predicted, expected)``
pairs so offline rescoring and live Langfuse scoring never disagree.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

from ._paired import require_equal_lengths
from .classification import ERROR_PREFIX, confusion_matrix, top_confusions
from .config import MAUD_CONSIDERATION_ALIASES, MAUD_CONSIDERATION_TYPES
from .corpus import (
    CORRESPONDENCE_SENTIMENT_LABELS,
    CORRESPONDENCE_TOPICS,
    MAUD_CLAUSE_CATEGORIES,
    MAUD_QUESTION_KEYS,
)
from .maud import canonical_maud_class, is_maud_class
from .tasks import normalize_maud_consideration
from .scorecard_honesty import detect_maud_gt_ambiguity
from .field_scoring import RETIRED_PROMPT_KEYS
from .gt_metadata import ANNOTATION_KEYS

__all__ = [
    "CORRESPONDENCE_CONTENT_KEYS",
    "MAUD_LABEL_KEY",
    "normalize_content_topic",
    "normalize_sentiment_label",
    "normalize_maud_question_key",
    "normalize_maud_category",
    "normalize_maud_answer",
    "is_valid_maud_answer",
    "parse_maud_labels",
    "peel_non_extraction_fields",
    "score_content_topic",
    "score_sentiment",
    "score_correspondence_content",
    "score_maud_extraction",
]

#: GT differentiator keys that are not CorrespondenceExtraction fields.
CORRESPONDENCE_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "content_topic",
        "topic_evidence",
        "sentiment_label",
        "sentiment_score",
        "sentiment_evidence",
        "label_evidence",
    }
)

#: Corpus JSON of {question: {answer, category, ...}} — not an extraction field.
MAUD_LABEL_KEY = "maud_clause_labels"

#: Keys stripped from extraction dicts before :func:`score_extraction`.
#: ``confidence`` / ``reasoning`` stay on the dict so :mod:`.trace_knobs`
#: can capture them; :func:`score_extraction` still skips scoring them.
#: Hub annotation stats and the stringified presence maps are stripped too
#: (they are never extraction targets on either side).
NON_EXTRACTION_KEYS: frozenset[str] = (
    CORRESPONDENCE_CONTENT_KEYS
    | {MAUD_LABEL_KEY}
    | RETIRED_PROMPT_KEYS
    | ANNOTATION_KEYS
)

_ALIAS_RE = re.compile(r"[^a-z0-9]+")

_SENTIMENT_ALIASES: dict[str, str] = {
    "pos": "positive",
    "positive": "positive",
    "plus": "positive",
    "neg": "negative",
    "negative": "negative",
    "minus": "negative",
    "neu": "neutral",
    "neutral": "neutral",
    "mixed": "neutral",
}

def _fold(value: Any) -> str:
    """Normalize a label to lowercase words using the content alias separators."""
    return _ALIAS_RE.sub(" ", str(value or "").strip().lower()).strip()


def _fold_key(value: Any) -> str:
    """Convert a folded content label to an underscore-separated key."""
    return _fold(value).replace(" ", "_")


def normalize_content_topic(value: Any) -> str:
    """Coerce a raw Enron topic into a canonical ``CORRESPONDENCE_TOPICS`` key."""
    if value is None or str(value).strip() == "":
        return ""
    folded = _fold_key(value)
    for topic in CORRESPONDENCE_TOPICS:
        if topic == folded or _fold(topic) == _fold(value):
            return topic
    return folded


def normalize_sentiment_label(value: Any) -> str:
    """Coerce a raw sentiment into ``negative`` / ``neutral`` / ``positive``."""
    if value is None or str(value).strip() == "":
        return ""
    folded = _fold(value)
    if folded in _SENTIMENT_ALIASES:
        return _SENTIMENT_ALIASES[folded]
    if folded in CORRESPONDENCE_SENTIMENT_LABELS:
        return folded
    return folded


def normalize_maud_question_key(value: Any) -> str:
    """Map a raw question string onto a Hub ``MAUD_QUESTION_KEYS`` entry."""
    folded = _fold(value)
    if not folded:
        return ""
    for key in MAUD_QUESTION_KEYS:
        if _fold(key) == folded:
            return key
    return str(value).strip()


def normalize_maud_category(value: Any) -> str:
    """Map a raw category onto ``MAUD_CLAUSE_CATEGORIES`` when recognised."""
    folded = _fold(value)
    if not folded:
        return ""
    for cat in MAUD_CLAUSE_CATEGORIES:
        if _fold(cat) == folded:
            return cat
    return folded.replace(" ", "_") if folded else ""


def normalize_maud_answer(
    question: str, value: Any, valid_classes: Sequence[str] | None = None
) -> str:
    """Task-aware answer fold: consideration token, else the class surface.

    A nonempty ``valid_classes`` supplies the GT record's own catalog;
    otherwise the corpus union
    (:data:`llm_dojo_scoring.maud.MAUD_ANSWER_CLASSES`) is the fallback.
    ``Type of Consideration`` ignores this catalog and uses consideration
    aliases, returning ``other`` for unrecognized values. ``None`` always
    returns an empty string.

    Other answers use :func:`llm_dojo_scoring.maud.canonical_maud_class`;
    unrecognized answers fall back to folded text. Normalization does not
    establish validity; use :func:`is_valid_maud_answer` to check it.
    """
    if value is None:
        return ""
    key = normalize_maud_question_key(question)
    if key == "Type of Consideration":
        return normalize_maud_consideration(value)
    return canonical_maud_class(key, value, valid_classes)


def is_valid_maud_answer(
    question: str, value: Any, valid_classes: Sequence[str] | None = None
) -> bool:
    """True when the answer uses only classes from the question's surface.

    Uses a nonempty ``valid_classes`` from the GT record, otherwise the
    corpus union for one of the 22 Hub questions. ``Type of Consideration``
    instead uses the configured consideration types and aliases, ignoring
    ``valid_classes``. ``None``, blank answers, and unknown questions without
    a nonempty catalog return ``False``.

    Other answers may match a whole class or multiple comma-separated
    classes after normalization. Yes/No aliases are accepted for whole
    answers only when the corresponding class exists in the catalog.
    """
    if value is None or str(value).strip() == "":
        return False
    key = normalize_maud_question_key(question)
    folded = _fold(value)
    if key == "Type of Consideration":
        if folded in MAUD_CONSIDERATION_ALIASES:
            return True
        return any(_fold(t) == folded for t in MAUD_CONSIDERATION_TYPES)
    if not valid_classes and key not in MAUD_QUESTION_KEYS:
        return False
    return is_maud_class(key, value, valid_classes)


def _macro_f1(expected: list[str], predicted: list[str]) -> float:
    """Unweighted mean F1 over labels that appear in *expected* (empty → 0)."""
    labels = sorted({e for e in expected if e})
    if not labels:
        return 0.0
    f1s: list[float] = []
    for label in labels:
        tp = sum(1 for e, p in zip(expected, predicted) if e == label and p == label)
        fp = sum(1 for e, p in zip(expected, predicted) if e != label and p == label)
        fn = sum(1 for e, p in zip(expected, predicted) if e == label and p != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1s.append(
            2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        )
    return round(sum(f1s) / len(f1s), 4)


def _label_content_score(
    task: str,
    expected: Iterable,
    predicted: Iterable,
    *,
    normalize,
) -> dict:
    """Score paired normalized labels, retaining error predictions as failures."""
    expected, predicted = list(expected), list(predicted)
    require_equal_lengths(expected, predicted)
    pairs = []
    for e, p in zip(expected, predicted):
        if str(p).startswith(ERROR_PREFIX):
            pairs.append((normalize(e), ERROR_PREFIX.strip().lower()))
            continue
        pairs.append((normalize(e), normalize(p)))
    exp = [e for e, _ in pairs]
    pred = [p for _, p in pairs]
    n = len(pairs)
    exact = round(sum(1.0 if e == p else 0.0 for e, p in pairs) / n, 4) if n else 0.0
    per_class: dict[str, dict] = {}
    for e, p in pairs:
        bucket = per_class.setdefault(e, {"n": 0, "correct": 0})
        bucket["n"] += 1
        bucket["correct"] += int(e == p)
    for bucket in per_class.values():
        bucket["accuracy"] = (
            round(bucket["correct"] / bucket["n"], 4) if bucket["n"] else 0.0
        )
    matrix, labels = confusion_matrix(exp, pred)
    return {
        "task": task,
        "kind": task,
        "exact_match": exact,
        "accuracy": exact,
        "f1_macro": _macro_f1(exp, pred),
        "per_class": per_class,
        "confusion": {"matrix": matrix, "labels": labels},
        "top_confusions": top_confusions(matrix, labels),
        "n": n,
    }


def _as_list(value: Any) -> list:
    """Convert sequences to lists, wrap scalars, and map None to an empty list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def score_content_topic(expected: Iterable, predicted: Iterable) -> dict:
    """Enron ``content_topic`` accuracy / macro-F1 over the 11 canonical topics."""
    result = _label_content_score(
        "enron_topic", expected, predicted, normalize=normalize_content_topic
    )
    result["content_topic_accuracy"] = result["accuracy"]
    result["content_topic_f1_macro"] = result["f1_macro"]
    return result


def score_sentiment(expected: Iterable, predicted: Iterable) -> dict:
    """Enron ``sentiment_label`` accuracy / macro-F1 over negative/neutral/positive."""
    result = _label_content_score(
        "enron_sentiment", expected, predicted, normalize=normalize_sentiment_label
    )
    result["sentiment_accuracy"] = result["accuracy"]
    result["sentiment_f1_macro"] = result["f1_macro"]
    return result


def score_correspondence_content(
    *,
    expected_topic: Any = None,
    predicted_topic: Any = None,
    expected_sentiment: Any = None,
    predicted_sentiment: Any = None,
) -> dict:
    """Combined Enron topic + sentiment score dict (omits sides that are absent)."""
    out: dict[str, Any] = {"task": "enron_content", "kind": "enron_content"}
    if expected_topic is not None and predicted_topic is not None:
        topic = score_content_topic(_as_list(expected_topic), _as_list(predicted_topic))
        out["topic"] = topic
        out["content_topic_accuracy"] = topic["content_topic_accuracy"]
        out["content_topic_f1_macro"] = topic["content_topic_f1_macro"]
        out["n_topic"] = topic["n"]
    if expected_sentiment is not None and predicted_sentiment is not None:
        sent = score_sentiment(
            _as_list(expected_sentiment), _as_list(predicted_sentiment)
        )
        out["sentiment"] = sent
        out["sentiment_accuracy"] = sent["sentiment_accuracy"]
        out["sentiment_f1_macro"] = sent["sentiment_f1_macro"]
        out["n_sentiment"] = sent["n"]
    return out


def _maybe_json(value: Any) -> Any:
    """Decode JSON-looking strings, leaving malformed JSON and other values intact."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _record_from_value(value: Any) -> dict[str, Any]:
    """Normalize a question's payload to an answer, category, and optional classes.

    Dictionary answers fall back to ``value`` or ``label`` when ``answer``
    is missing or ``None``. Lists and tuples drop ``None`` and empty strings
    and unwrap a single remaining item; scalar answers become strings.
    ``valid_classes`` is included only when nonempty after filtering, with
    entries converted to strings and a string treated as one class.
    A truthy, non-iterable ``valid_classes`` raises ``TypeError``.
    """
    if isinstance(value, dict):
        answer = value.get("answer")
        if answer is None:
            answer = value.get("value") or value.get("label") or ""
        category = value.get("category") or ""
        valid = value.get("valid_classes") or ()
        if isinstance(valid, str):
            valid = [valid]
        valid = [str(item) for item in valid if item not in (None, "")]
        rec: dict[str, Any] = {}
        if isinstance(answer, (list, tuple)):
            items = [item for item in answer if item not in (None, "")]
            rec["answer"] = items[0] if len(items) == 1 else list(items)
        else:
            rec["answer"] = str(answer)
        rec["category"] = str(category)
        if valid:
            rec["valid_classes"] = valid
        return rec
    return {"answer": "" if value is None else str(value), "category": ""}


#: Hub keys as whitespace-tolerant prefixes, longest first. The Hub spells
#: "Fiduciary exception:  Board determination (no-shop)" with two spaces; a
#: model emitting one space used to split at the first colon into an unknown
#: "Fiduciary exception" question and lose the answer.
_SPAN_PREFIXES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (key, re.compile(r"\s+".join(re.escape(w) for w in key.split()) + r"\s*:", re.IGNORECASE))
    for key in sorted(MAUD_QUESTION_KEYS, key=len, reverse=True)
)


def _span_lines(value: Any) -> list[Any]:
    """Split a newline-joined block of spans into one span per line."""
    if isinstance(value, str) and "\n" in value:
        return [line for line in value.splitlines() if line.strip()]
    return [value]


def _parse_span(span: str) -> tuple[str, str] | None:
    """Split ``'<Question>: <Answer>'`` preferring the longest Hub key prefix."""
    text = str(span).strip()
    if not text:
        return None
    for key, pattern in _SPAN_PREFIXES:
        match = pattern.match(text)
        if match:
            return key, text[match.end():].strip()
    if ":" in text:
        question, answer = text.split(":", 1)
        return question.strip(), answer.strip()
    return None


def _coalesce_maud_answers(answers: list[Any]) -> Any:
    """Dedupe by fold; keep a list when several distinct answers remain (#19)."""
    seen: set[str] = set()
    unique: list[Any] = []
    for ans in answers:
        folded = _fold(ans)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        unique.append(ans)
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return unique


def reduce_maud_prediction(
    question: str, answer: Any, valid_classes: Sequence[str] | None = None
) -> tuple[Any, bool]:
    """Collapse a multi-answer *prediction* for one question.

    Chunked specialists answer the full schema on every chunk, and the merge
    keeps each distinct string, so a question can arrive as a list such as
    ``["not specified", "Yes"]``. Off-catalog entries are dropped when at
    least one in-catalog answer exists; if the in-catalog answers then share
    one canonical class, that class is the prediction. Distinct in-catalog
    answers stay a list (scored wrong) and are reported as a conflict, never
    resolved by position. Returns ``(answer, conflict)``.
    """
    if not isinstance(answer, (list, tuple)):
        return answer, False
    items = [a for a in answer if a not in (None, "") and str(a).strip()]
    if len(items) <= 1:
        return (items[0] if items else ""), False
    valid = [a for a in items if is_valid_maud_answer(question, a, valid_classes)]
    pool = valid or items
    canon: dict[str, Any] = {}
    for a in pool:
        canon.setdefault(normalize_maud_answer(question, a, valid_classes), a)
    if len(canon) == 1:
        return next(iter(canon.values())), False
    return list(canon.values()), True


def _merge_maud_record(out: dict[str, dict[str, Any]], key: str, rec: dict[str, Any]) -> None:
    """Same Hub key with distinct answers → list (ambiguous), never last-wins.

    ``valid_classes`` from repeated records are unioned so no row-level
    class surface is dropped.
    """
    if key not in out:
        out[key] = rec
        return
    existing = out[key]
    combined: list[Any] = []
    for src in (existing.get("answer"), rec.get("answer")):
        if isinstance(src, (list, tuple)):
            combined.extend(src)
        elif src not in (None, ""):
            combined.append(src)
    existing["answer"] = _coalesce_maud_answers(combined)
    if not existing.get("category") and rec.get("category"):
        existing["category"] = rec["category"]
    if rec.get("gt_ambiguous") or rec.get("ambiguous"):
        existing["gt_ambiguous"] = True
    merged_valid: list[str] = []
    seen_valid: set[str] = set()
    for src in (existing.get("valid_classes") or (), rec.get("valid_classes") or ()):
        for cls in src:
            if cls not in seen_valid:
                seen_valid.add(cls)
                merged_valid.append(cls)
    if merged_valid:
        existing["valid_classes"] = merged_valid


def parse_maud_labels(value: Any) -> dict[str, dict[str, Any]]:
    """Coerce corpus JSON, specialist spans, or a question→answer map.

    Returns records keyed by canonical question, with ``answer``,
    ``category``, and optional nonempty ``valid_classes``. Distinct
    sub-question keys stay distinct. Repeated Hub keys with different
    answers keep every distinct answer so the scorer can mark
    ``gt_ambiguous`` instead of silently keeping the last span; their
    class lists are unioned.

    ``None`` and values with no parseable question return an empty dict.
    Malformed JSON is treated as span text, not raised as a decoding error.
    A record with truthy, non-iterable ``valid_classes`` raises ``TypeError``.
    """
    value = _maybe_json(value)
    if value is None:
        return {}
    if isinstance(value, dict):
        out: dict[str, dict[str, Any]] = {}
        for raw_key, raw_val in value.items():
            key = normalize_maud_question_key(raw_key)
            if not key:
                continue
            if raw_val is None:
                # {question: null} is the object grammar's "not answered" —
                # absent, not an invalid class. ("" stays present-but-empty.)
                continue
            rec = _record_from_value(raw_val)
            rec["category"] = normalize_maud_category(rec["category"]) if rec["category"] else ""
            if isinstance(raw_val, dict) and (
                raw_val.get("gt_ambiguous") or raw_val.get("ambiguous")
            ):
                rec["gt_ambiguous"] = True
            _merge_maud_record(out, key, rec)
        return out
    if isinstance(value, str) and "\n" in value:
        value = _span_lines(value)
    if isinstance(value, (list, tuple)):
        out = {}
        for item in (line for raw in value for line in _span_lines(raw)):
            if isinstance(item, dict) and ("question" in item or "key" in item):
                key = normalize_maud_question_key(item.get("question") or item.get("key"))
                rec = _record_from_value(item)
                rec["category"] = (
                    normalize_maud_category(rec["category"]) if rec["category"] else ""
                )
                if key:
                    _merge_maud_record(out, key, rec)
                continue
            parsed = _parse_span(item)
            if parsed is None:
                continue
            question, answer = parsed
            key = normalize_maud_question_key(question)
            if key:
                _merge_maud_record(out, key, {"answer": answer, "category": ""})
        return out
    parsed = _parse_span(str(value))
    if parsed is None:
        return {}
    question, answer = parsed
    key = normalize_maud_question_key(question)
    return {key: {"answer": answer, "category": ""}} if key else {}


def _documents(expected: Any, predicted: Any) -> list[tuple[Any, Any]]:
    """Return a list of ``(expected, predicted)`` document pairs.

    Decode JSON object/array strings; malformed JSON remains unchanged.
    A list beginning with a dict is a batch, even with later non-dict
    entries. If neither side is a batch, return a single pair, keeping
    span lists (including empty lists) as one document.

    When either side is a batch, pair list entries by position. Raise
    ``ValueError`` for unequal lengths when both sides are lists and each
    is empty or begins with a dict. Other list pairings stop at the
    shorter side. A non-list expected value is one document; a non-list
    prediction is reused for every expected document.
    """
    exp = _maybe_json(expected)
    pred = _maybe_json(predicted)
    exp_batch = isinstance(exp, list) and exp and isinstance(exp[0], dict)
    pred_batch = isinstance(pred, list) and pred and isinstance(pred[0], dict)
    if exp_batch or pred_batch:
        if (isinstance(exp, list) and isinstance(pred, list)
                and (not exp or exp_batch) and (not pred or pred_batch)):
            require_equal_lengths(exp, pred)
        exp_list = exp if isinstance(exp, list) else [exp]
        pred_list = pred if isinstance(pred, list) else [pred] * len(exp_list)
        return list(zip(exp_list, pred_list))
    return [(exp, pred)]


def score_maud_extraction(expected: Any, predicted: Any) -> dict:
    """Per-question MAUD extraction over the 22 Hub keys plus distinct extras.

    Accepts label maps, JSON, or clause spans via :func:`parse_maud_labels`.
    A list beginning with a dict is treated as a document batch; a list of
    strings is one document's spans. Unequal lists recognized as document
    batches on both sides raise ValueError; batch-versus-span forms retain their pairing
    behavior. A non-list prediction is reused for each expected document.
    Label parsing errors propagate.

    Headlines:

    - ``maud_question_accuracy`` — micro exact-answer match over **clean** questions
    - ``maud_question_macro_accuracy`` — unweighted mean of per-question accuracy
    - ``maud_clause_presence`` — share of clean expected questions with a predicted record
    - ``maud_valid_class_rate`` — share of those present predictions with valid answers
    - ``maud_category_accuracy`` — match over expected categories; missing predictions miss
    - ``n_ambiguous`` — collapsed multi-answer keys marked ``gt_ambiguous`` / unscorable
    - ``maud_precision_answered`` — exact matches over questions the model answered
    - ``n_pred_conflicts`` — predicted questions left with distinct in-catalog
      answers after :func:`reduce_maud_prediction` (scored wrong)

    Ambiguous expected answers are excluded from all metric denominators;
    prediction-only questions are ignored. Answer normalization and validity
    use the expected record's nonempty class catalog, with the fallback and
    consideration exception described in :func:`is_valid_maud_answer`.
    Exact matching is independent of class validity.

    Returns headline rates rounded to four decimals (``None`` when their
    denominator is zero), counts, and per-question and per-document details.
    Status is ``unscorable`` only when there are ambiguous expected questions
    and no clean ones.
    """
    docs = _documents(expected, predicted)
    question_stats: dict[str, dict[str, int]] = {
        key: {"n": 0, "exact": 0, "present": 0, "valid": 0, "category_n": 0, "category_ok": 0}
        for key in MAUD_QUESTION_KEYS
    }
    extra_questions: dict[str, dict[str, int]] = {}
    n_expected = 0
    n_exact = 0
    n_present = 0
    n_valid = 0
    n_category = 0
    n_category_ok = 0
    n_ambiguous = 0
    n_pred_conflicts = 0
    ambiguous_counts: dict[str, int] = {}
    per_doc: list[dict] = []

    for exp_raw, pred_raw in docs:
        exp_labels = parse_maud_labels(exp_raw)
        pred_labels = parse_maud_labels(pred_raw)
        ambiguous_keys = detect_maud_gt_ambiguity(exp_labels)
        doc_n = 0
        doc_exact = 0
        doc_present = 0
        doc_ambiguous: list[str] = []
        for question, rec in exp_labels.items():
            if question in ambiguous_keys:
                n_ambiguous += 1
                ambiguous_counts[question] = ambiguous_counts.get(question, 0) + 1
                doc_ambiguous.append(question)
                continue
            stats = question_stats.get(question)
            if stats is None:
                stats = extra_questions.setdefault(
                    question,
                    {"n": 0, "exact": 0, "present": 0, "valid": 0, "category_n": 0, "category_ok": 0},
                )
            stats["n"] += 1
            n_expected += 1
            doc_n += 1
            pred_rec = pred_labels.get(question)
            present = pred_rec is not None
            # The GT record's own catalog is the authority for this row;
            # the corpus union is only the fallback (see llm_dojo_scoring.maud).
            gt_classes = rec.get("valid_classes")
            if present:
                stats["present"] += 1
                n_present += 1
                doc_present += 1
                pred_answer, conflict = reduce_maud_prediction(
                    question, pred_rec["answer"], gt_classes
                )
                n_pred_conflicts += int(conflict)
                if is_valid_maud_answer(question, pred_answer, gt_classes):
                    stats["valid"] += 1
                    n_valid += 1
            else:
                pred_answer = ""
            exp_answer = normalize_maud_answer(question, rec["answer"], gt_classes)
            got_answer = (
                normalize_maud_answer(question, pred_answer, gt_classes)
                if present
                else ""
            )
            if present and exp_answer == got_answer:
                stats["exact"] += 1
                n_exact += 1
                doc_exact += 1
            exp_cat = rec.get("category") or ""
            pred_cat = (pred_rec or {}).get("category") or ""
            if exp_cat:
                stats["category_n"] += 1
                n_category += 1
                if pred_cat and normalize_maud_category(pred_cat) == exp_cat:
                    stats["category_ok"] += 1
                    n_category_ok += 1
        per_doc.append({
            "n_questions": doc_n,
            "exact": doc_exact,
            "present": doc_present,
            "accuracy": round(doc_exact / doc_n, 4) if doc_n else 0.0,
            "presence": round(doc_present / doc_n, 4) if doc_n else 0.0,
            "n_ambiguous": len(doc_ambiguous),
            "gt_ambiguous_keys": doc_ambiguous,
        })

    per_question: dict[str, dict] = {}
    question_accuracies: list[float] = []
    for question, stats in {**question_stats, **extra_questions}.items():
        if stats["n"] == 0:
            continue
        acc = round(stats["exact"] / stats["n"], 4)
        question_accuracies.append(acc)
        per_question[question] = {
            "n": stats["n"],
            "accuracy": acc,
            "presence": round(stats["present"] / stats["n"], 4),
            "valid_class_rate": (
                round(stats["valid"] / stats["present"], 4) if stats["present"] else 0.0
            ),
            "category_accuracy": (
                round(stats["category_ok"] / stats["category_n"], 4)
                if stats["category_n"] else None
            ),
            "status": "scored",
            "gt_ambiguous": False,
        }
    for question, count in ambiguous_counts.items():
        bucket = per_question.get(question)
        if bucket is None:
            per_question[question] = {
                "n": 0,
                "accuracy": None,
                "presence": None,
                "valid_class_rate": None,
                "category_accuracy": None,
                "status": "unscorable",
                "gt_ambiguous": True,
                "n_ambiguous": count,
            }
        else:
            bucket["gt_ambiguous"] = True
            bucket["n_ambiguous"] = count

    all_ambiguous = n_expected == 0 and n_ambiguous > 0
    return {
        "task": "maud_extraction",
        "kind": "maud_extraction",
        "status": "unscorable" if all_ambiguous else "scored",
        "reason": "gt_ambiguous" if all_ambiguous else None,
        "gt_ambiguous": all_ambiguous,
        "maud_question_accuracy": round(n_exact / n_expected, 4) if n_expected else None,
        "maud_question_macro_accuracy": (
            round(sum(question_accuracies) / len(question_accuracies), 4)
            if question_accuracies else None
        ),
        "maud_clause_presence": round(n_present / n_expected, 4) if n_expected else None,
        "maud_valid_class_rate": round(n_valid / n_present, 4) if n_present else None,
        "maud_category_accuracy": (
            round(n_category_ok / n_category, 4) if n_category else None
        ),
        "n_questions": n_expected,
        "n_ambiguous": n_ambiguous,
        "ambiguous_keys": sorted(ambiguous_counts),
        "n_documents": len(docs),
        "n_present": n_present,
        "n_pred_conflicts": n_pred_conflicts,
        "maud_precision_answered": round(n_exact / n_present, 4) if n_present else None,
        "metric_id": "maud.question.micro_accuracy",
        "per_question": per_question,
        "per_document": per_doc,
    }


def peel_non_extraction_fields(
    expected: dict, predicted: dict
) -> tuple[dict, dict, dict[str, Any]]:
    """Split correspondence/MAUD differentiators off an extraction pair.

    Returns ``(expected_fields, predicted_fields, payload)`` where *payload*
    holds optional ``content_topic``, ``sentiment_label``, and ``maud`` pairs
    used by the content scorers. Evidence columns are dropped, not scored.
    """
    exp_fields = {k: v for k, v in expected.items() if k not in NON_EXTRACTION_KEYS}
    pred_fields = {k: v for k, v in predicted.items() if k not in NON_EXTRACTION_KEYS}
    payload: dict[str, Any] = {}
    if "content_topic" in expected or "content_topic" in predicted:
        payload["content_topic"] = (
            expected.get("content_topic"),
            predicted.get("content_topic"),
        )
    if "sentiment_label" in expected or "sentiment_label" in predicted:
        payload["sentiment_label"] = (
            expected.get("sentiment_label"),
            predicted.get("sentiment_label"),
        )
    exp_maud = expected.get(MAUD_LABEL_KEY)
    pred_maud = predicted.get(MAUD_LABEL_KEY)
    # ``maud_clauses`` is an extraction field; only use it as the prediction
    # (or expected) side when the corpus differentiator is present.
    if exp_maud is not None or pred_maud is not None:
        if exp_maud is None:
            exp_maud = expected.get("maud_clauses")
        if pred_maud is None:
            pred_maud = predicted.get("maud_clauses")
        payload["maud"] = (exp_maud, pred_maud)
    return exp_fields, pred_fields, payload
