"""Closed-label vocabularies and the out-of-vocabulary check (#285).

Several extraction fields are closed labels: the Dojo scores anything off the list as
wrong. ``taxonomy.yaml`` (``doc_classes[].label_vocabularies``) is the single source of
truth for each list. After extraction ``out_of_vocabulary_fields`` finds the values that are
not on their list, and routing sends such a document to review (cause ``out_of_vocabulary``)
instead of archiving it unflagged. The judge does not reliably catch these, so the check is
deterministic.

A value is compared after folding case, surrounding whitespace and runs of whitespace,
``_`` and ``-`` to one separator, so ``"All Cash"`` and ``"ALL_CASH"`` are ``all_cash`` and
``"Time Sensitive"`` is ``time-sensitive``. A value that matches is rewritten to its
canonical token (``canonicalize_labels``); one that does not is left as the model wrote it, so
a reviewer sees what was extracted. A missing or blank value is not off-list: leaving a field
empty is a valid answer. The taxonomy is ``lru_cache``d; edits need a restart.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_SEPARATORS = re.compile(r"[\s_\-]+")


def _fold(value: Any) -> str:
    return _SEPARATORS.sub("_", str(value).strip().lower()).strip("_")


def get_label_vocabularies(doc_type: str | None) -> dict[str, tuple[str, ...]]:
    """``{field: allowed values}`` for the class ``doc_type`` extracts as; ``{}`` when none."""
    from pipeline.config import get_doc_class, resolve_extract_class

    key = resolve_extract_class(doc_type)
    cls = get_doc_class(key) if key else None
    declared = (cls or {}).get("label_vocabularies") or {}
    out: dict[str, tuple[str, ...]] = {}
    for field, values in declared.items():
        if isinstance(values, (list, tuple)) and values:
            out[str(field)] = tuple(str(v) for v in values)
    return out


def canonical_label_value(vocabulary: tuple[str, ...], value: Any) -> str | None:
    """The vocabulary token ``value`` folds onto, or None when it is off the list."""
    if value is None or isinstance(value, (bool, list, tuple, dict, set)):
        return None
    folded = _fold(value)
    if not folded:
        return None
    for token in vocabulary:
        if _fold(token) == folded:
            return token
    return None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value in ([], {})


def out_of_vocabulary_fields(doc_type: str | None, extracted: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{field: value}`` for each closed-label field whose non-blank value is off its list."""
    if not isinstance(extracted, Mapping):
        return {}
    off: dict[str, Any] = {}
    for field, vocabulary in get_label_vocabularies(doc_type).items():
        value = extracted.get(field)
        if _is_blank(value):
            continue
        if canonical_label_value(vocabulary, value) is None:
            off[field] = value
    return off


def canonicalize_labels(doc_type: str | None, extracted: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of ``extracted`` with each on-list closed-label value written as its canonical token."""
    result = dict(extracted or {})
    for field, vocabulary in get_label_vocabularies(doc_type).items():
        value = result.get(field)
        if isinstance(value, str):
            token = canonical_label_value(vocabulary, value)
            if token is not None and token != value:
                result[field] = token
    return result
