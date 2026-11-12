"""Read mailroom-dataset v10 typed rows with the scorers built for ``gt_fields``.

v10 (built in ``Exios66/Mailroom-Corpus-EDA``, ``v10_build``) publishes one typed
config per class (``gt_contract``, ``gt_merger_agreement``, ``gt_correspondence``,
``gt_insurance_claim``, ``gt_corporate_record``). Each row has:

- identity columns: ``document_id``, ``filename``, ``subclass`` (the canonical key),
  ``source_subclass`` (the raw v9 value), ``content_sha256``,
  ``source_content_sha256``;
- one typed column per schema field (money a float, entity lists a list of
  strings, ``cuad_clauses`` a list of ``{category, start, end, text}`` spans,
  ``maud_clauses`` a list of ``{question, category, answer}`` answers);
- ``presence``: ``{field: code}`` with the v9 codes (``populated``,
  ``schema_documented_absence``, ``not_applicable``, ``pending_annotation``);
- ``evidence``: ``{field: [{start, end, text}]}``; ``provenance``: ``{field: str}``.

The legacy ``ground_truth`` config (deprecated in v10) keeps the v9 shape. Every
scorer reads that shape, so :func:`gt_from_v10_row` maps a typed row onto it and a
v10 row scores exactly like the legacy row it came from. The long ``cuad_clauses``
and ``maud_answers`` configs pivot back to per-document label maps with
:func:`cuad_labels_from_long` / :func:`maud_labels_from_long`, and
:func:`coverage_report` counts presence codes per field.

Rows may come from ``datasets`` / pandas / pyarrow, so array-likes (numpy arrays,
tuples) are lists here and missing struct members (``None``) are dropped. Stdlib
only, like :mod:`.gt_metadata`.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Iterable, Mapping

from .doc_classes import LIVE_DOC_TYPES, in_order
from .gt_metadata import GT_PRESENCE_KEY

__all__ = [
    "IDENTITY_COLUMNS",
    "PRESENCE_CODES",
    "V10_GT_CONFIGS",
    "V10_LEGACY_KEYS",
    "coverage_report",
    "cuad_labels_from_long",
    "gt_from_v10_row",
    "maud_labels_from_long",
]

#: Columns of a ``gt_<class>`` row that identify it; they are not GT fields.
IDENTITY_COLUMNS: tuple[str, ...] = (
    "document_id",
    "filename",
    "subclass",
    "source_subclass",
    "content_sha256",
    "source_content_sha256",
)

#: Struct columns of a ``gt_<class>`` row (presence codes and label support).
_STRUCT_COLUMNS: tuple[str, ...] = ("presence", "evidence", "provenance")

#: The four ``gt_presence`` codes, in report order.
PRESENCE_CODES: tuple[str, ...] = (
    "populated",
    "schema_documented_absence",
    "not_applicable",
    "pending_annotation",
)

#: Typed v10 field -> the legacy ``gt_fields`` key that carries it.
V10_LEGACY_KEYS: dict[str, str] = {
    "cuad_clauses": "cuad_clause_labels",
    "maud_clauses": "maud_clause_labels",
}

#: Document class -> its v10 typed config name.
V10_GT_CONFIGS: dict[str, str] = {
    cls: f"gt_{cls}"
    for cls in in_order(
        LIVE_DOC_TYPES, ("contract", "merger_agreement", "correspondence", "insurance_claim", "corporate_record")
    )
}


def _plain(value: Any) -> Any:
    """Plain Python for one cell: array-likes become lists, structs dicts, NaN ``None``."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if type(value).__name__ in ("NAType", "NaTType"):  # pandas.NA / pandas.NaT
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):  # numpy arrays and scalars, pyarrow scalars
        return _plain(tolist())
    as_py = getattr(value, "as_py", None)
    if callable(as_py):
        return _plain(as_py())
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _struct(row: Mapping[str, Any], column: str) -> dict[str, Any]:
    """A struct column as a dict with ``None`` members dropped (``{}`` when absent)."""
    raw = _plain(row.get(column))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): v for k, v in raw.items() if v is not None}


def _cuad_map(spans: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``[{category, start, text, ...}]`` -> the legacy ``{category: [{start, text}]}``."""
    out: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        if not isinstance(span, Mapping) or not span.get("category"):
            continue
        out.setdefault(str(span["category"]), []).append(
            {"start": int(span.get("start") or 0), "text": str(span.get("text") or "")}
        )
    return out


def _maud_map(answers: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """``[{question, category, answer}]`` -> the legacy ``{question: {category, answer}}``."""
    out: dict[str, dict[str, str]] = {}
    for item in answers:
        if not isinstance(item, Mapping) or not item.get("question"):
            continue
        out[str(item["question"])] = {
            "category": str(item.get("category") or ""),
            "answer": str(item.get("answer") or ""),
        }
    return out


def gt_from_v10_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The legacy ``gt_fields`` dict for one typed ``gt_<class>`` row.

    Each schema field with a value is copied under its legacy key (``cuad_clauses``
    becomes ``cuad_clause_labels`` as ``{category: [{start, text}]}``,
    ``maud_clauses`` becomes ``maud_clause_labels`` as ``{question: {category,
    answer}}``). ``presence`` becomes ``gt_presence`` under the same keys, with
    ``None`` codes dropped. ``evidence`` and ``provenance`` are kept as annotation
    keys (never scored), and ``source_subclass`` becomes ``expected_subclass``, the
    legacy subclass column. Identity columns are left out. Returns a new dict; a
    non-mapping row raises ``TypeError``.
    """
    if not isinstance(row, Mapping):
        raise TypeError(f"a v10 row must be a mapping, got {type(row).__name__}")
    presence = _struct(row, "presence")
    out: dict[str, Any] = {}
    for column, raw in row.items():
        if column in IDENTITY_COLUMNS or column in _STRUCT_COLUMNS:
            continue
        value = _plain(raw)
        if value is None or value == [] or value == {}:
            continue
        if column == "cuad_clauses":
            value = _cuad_map(value)
        elif column == "maud_clauses":
            value = _maud_map(value)
        out[V10_LEGACY_KEYS.get(column, column)] = value
    out[GT_PRESENCE_KEY] = {
        V10_LEGACY_KEYS.get(field, field): str(code) for field, code in presence.items()
    }
    for column in ("evidence", "provenance"):
        support = _struct(row, column)
        if support:
            out[column] = support
    source_subclass = _plain(row.get("source_subclass"))
    if source_subclass not in (None, ""):
        out["expected_subclass"] = str(source_subclass)
    return out


def cuad_labels_from_long(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Pivot the long ``cuad_clauses`` config: ``{document_id: {category: [{start, text}]}}``.

    Spans keep their input order within a category. Rows without a
    ``document_id`` or ``category`` are skipped.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        doc = _plain(row.get("document_id"))
        if doc:
            grouped.setdefault(str(doc), []).append({k: _plain(v) for k, v in row.items()})
    return {doc: _cuad_map(spans) for doc, spans in grouped.items()}


def maud_labels_from_long(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, str]]]:
    """Pivot the long ``maud_answers`` config: ``{document_id: {question: {category, answer}}}``.

    A repeated question keeps its last answer. Rows without a ``document_id`` or
    ``question`` are skipped.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        doc = _plain(row.get("document_id"))
        if doc:
            grouped.setdefault(str(doc), []).append({k: _plain(v) for k, v in row.items()})
    return {doc: _maud_map(answers) for doc, answers in grouped.items()}


def coverage_report(
    rows: Iterable[Mapping[str, Any]], *, fields: Iterable[str] = ()
) -> dict[str, dict[str, Any]]:
    """Per-field presence counts over typed ``gt_<class>`` rows.

    ``fields`` (for example ``DEFAULT_FIELD_TYPES[doc_type]``) are always reported,
    so a field no row carries a code for shows up as all ``missing`` instead of being
    left out of the report.

    Returns ``{field: {n_rows, populated, schema_documented_absence,
    not_applicable, pending_annotation, missing, applicable, coverage}}``, fields in
    first-seen order. ``missing`` counts rows whose ``presence`` has no code for the
    field; ``applicable`` is every row not ``not_applicable``; ``coverage`` is
    (``populated`` + ``schema_documented_absence``) / ``applicable``, ``None`` when
    nothing applies. The v10 publish gate (coverage residue 0) is ``coverage == 1.0``
    on every field.
    """
    counts: dict[str, Counter[str]] = {str(f): Counter() for f in fields}
    n_rows = 0
    for row in rows:
        n_rows += 1
        for field, code in _struct(row, "presence").items():
            counts.setdefault(field, Counter())[str(code)] += 1
    out: dict[str, dict[str, Any]] = {}
    for field, c in counts.items():
        coded = sum(c[code] for code in PRESENCE_CODES)
        entry: dict[str, Any] = {"n_rows": n_rows, **{code: c[code] for code in PRESENCE_CODES}}
        entry["missing"] = n_rows - coded
        applicable = n_rows - c["not_applicable"]
        entry["applicable"] = applicable
        labeled = c["populated"] + c["schema_documented_absence"]
        entry["coverage"] = round(labeled / applicable, 6) if applicable else None
        out[field] = entry
    return out
