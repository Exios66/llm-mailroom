"""Extraction schemas for the five mailroom classes, per document type and subtype.

One registry covering every class the mailroom routes — ``contract``,
``merger_agreement``, ``corporate_record``, ``correspondence`` and
``insurance_claim`` — at two levels:

* a **document-type schema** (``extraction_schema("insurance_claim")``): the
  class's live field map with each field's scoring status over the whole class;
* a **subtype schema** (``extraction_schema("insurance_claim", "pde")``) for
  every subtype in :data:`~llm_dojo_scoring.schemas.corpus.DOC_TYPE_SUBCLASSES`:
  the same fields with the status that subtype's ground truth supports, plus the
  subtype's own fields and types (``cms_source_table`` and identifier-aware
  ``supporting_documents`` on CMS tables, the subclass entities of a corporate
  record) and its salient guidance (CUAD categories for a contract family, MAUD
  priority questions for a consideration type).

Field status is read from the mailroom-dataset v9 lineage
(``data/extraction_status.json``, frozen from the Hub tag recorded in it by
``scripts/build_extraction_status.py``), never guessed:

``scored``
    GT populates the field on (nearly) every row: scored on every document.
``partial``
    GT populates it on some rows (e.g. ``adjuster`` on auto claims): scored
    where the row labels it.
``derived``
    Truth comes from a label source, not from ``gt_fields``: CUAD clause labels
    (contract metadata fields), MAUD clause labels, or the subtype itself
    (``cuad_family``, ``merger_consideration``, ``record_type``,
    ``communication_type``, ``cms_source_table``).
``documented_absent``
    The schema documents the field as absent for this subtype: an empty answer
    is right and a stated value is a false positive (CMS ``adjuster``).
``label_task``
    Populated GT scored by a label task, not by extraction
    (correspondence ``content_topic`` / ``sentiment_label``).
``pending_annotation``
    In the live field map but GT does not label it yet: never an expected
    event, reported as unscored.
``not_applicable``
    GT marks it not applicable to this class (merger ``intent``).

Nothing here changes a score: the scorers already skip fields the row does
not label. The registry names what each schema scores, so prompts, guided
decoding, scorecards and coverage reports share one definition.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .corpus import DOC_TYPE_SUBCLASSES, normalize_corpus_subclass

__all__ = [
    "MAILROOM_DOC_TYPES",
    "FIELD_STATUSES",
    "SCORED_STATUSES",
    "FieldSchema",
    "ExtractionSchema",
    "extraction_status_source",
    "extraction_schema",
    "extraction_subtypes",
    "iter_extraction_schemas",
    "schema_coverage",
    "registry_violations",
]

#: The five classes the mailroom routes and scores (mailroom-dataset v9+).
#: Equal to ``pipeline.mailroom.LIVE_DOC_TYPES`` (checked by
#: :func:`registry_violations`; not imported here to keep ``schemas`` below
#: ``pipeline``).
MAILROOM_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)

FIELD_STATUSES: tuple[str, ...] = (
    "scored",
    "partial",
    "derived",
    "documented_absent",
    "label_task",
    "pending_annotation",
    "not_applicable",
)
#: Statuses whose fields count toward an extraction score when GT carries them.
SCORED_STATUSES: frozenset[str] = frozenset({"scored", "partial", "derived", "documented_absent"})

_DATA_PATH = Path(__file__).with_name("data") / "extraction_status.json"

#: Share of rows at or above which a populated field is ``scored`` (else ``partial``).
_SCORED_SHARE = 0.95

#: CUAD category each contract metadata field is scored against
#: (``pipeline.sandbox_scoring.FIELD_CATEGORY``); all read ``cuad_clause_labels``.
_CUAD_DERIVED: dict[str, str] = {
    "document_name": "Document Name",
    "parties": "Parties",
    "effective_date": "Effective Date",
    "governing_law": "Governing Law",
    "term_length": "Expiration Date",
    "renewal_terms": "Renewal Term",
    "cuad_clauses": "all CUAD categories",
}

#: Fields whose truth is the row's subtype itself.
_SUBTYPE_DERIVED: dict[str, str] = {
    "contract": "cuad_family",
    "merger_agreement": "merger_consideration",
    "corporate_record": "record_type",
    "correspondence": "communication_type",
}

#: Hub label columns that CUAD / MAUD derived fields read their truth from.
_LABEL_SOURCES: frozenset[str] = frozenset({"cuad_clause_labels", "maud_clause_labels"})

#: Fields one class's live map carries for another class (never this class's truth).
_FOREIGN: dict[str, frozenset[str]] = {
    "contract": frozenset({"merger_consideration", "maud_clauses"}),
}

#: GT fields scored by a label task rather than by extraction.
_LABEL_TASK_FIELDS: dict[str, dict[str, str]] = {
    "correspondence": {"content_topic": "label", "sentiment_label": "label"},
}


@dataclass(frozen=True)
class FieldSchema:
    """One field of an extraction schema."""

    name: str
    field_type: str
    status: str
    #: Where truth comes from: ``gt_fields``, a label source, or ``subtype``.
    source: str
    #: Share of the schema's rows whose GT labels the field (``None`` with no rows).
    coverage: float | None = None
    note: str = ""

    @property
    def scored(self) -> bool:
        """Whether the field counts toward the extraction score when labelled."""
        return self.status in SCORED_STATUSES


@dataclass(frozen=True)
class ExtractionSchema:
    """The extraction schema of one document type, or of one of its subtypes."""

    doc_type: str
    subtype: str | None
    rows: int
    fields: Mapping[str, FieldSchema]
    #: Subtype guidance: salient CUAD categories, MAUD priority questions, or
    #: the corporate subclass's entity fields. Empty for a document-type schema.
    salient: tuple[str, ...] = ()
    revision: str = ""
    extensions: tuple[str, ...] = field(default=())

    @property
    def schema_id(self) -> str:
        """``doc_type`` or ``doc_type/subtype``, with the dataset revision."""
        base = self.doc_type if self.subtype is None else f"{self.doc_type}/{self.subtype}"
        return f"{base}@{self.revision}" if self.revision else base

    def _with(self, statuses: frozenset[str] | set[str]) -> tuple[str, ...]:
        """Field names whose status is in ``statuses``, in field order."""
        return tuple(name for name, spec in self.fields.items() if spec.status in statuses)

    @property
    def field_types(self) -> dict[str, str]:
        """Field types for extraction: every field except label-task and n/a ones."""
        return {
            name: spec.field_type
            for name, spec in self.fields.items()
            if spec.status not in ("label_task", "not_applicable")
        }

    @property
    def scored_fields(self) -> tuple[str, ...]:
        """Fields that count toward the score when the row labels them."""
        return self._with(SCORED_STATUSES)

    @property
    def documented_absent_fields(self) -> tuple[str, ...]:
        """Fields whose right answer is empty for this schema."""
        return self._with({"documented_absent"})

    @property
    def pending_fields(self) -> tuple[str, ...]:
        """Live-map fields GT does not label yet (reported, never scored)."""
        return self._with({"pending_annotation"})

    def status_counts(self) -> dict[str, int]:
        """How many fields carry each status."""
        counts = {status: 0 for status in FIELD_STATUSES}
        for spec in self.fields.values():
            counts[spec.status] += 1
        return {status: n for status, n in counts.items() if n}

    def json_schema(self) -> dict[str, Any]:
        """A JSON Schema object for guided decoding of this schema's fields."""
        from .scored_enums import SCORED_ENUM_FIELDS, scored_enum_schema

        enums = SCORED_ENUM_FIELDS.get(self.doc_type, {})
        properties: dict[str, Any] = {}
        for name, field_type in self.field_types.items():
            if name in enums:
                properties[name] = scored_enum_schema(self.doc_type, name)
            else:
                properties[name] = _json_for(field_type)
        return {
            "type": "object",
            "title": self.schema_id,
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready summary (for provenance and reports)."""
        return {
            "schema_id": self.schema_id,
            "doc_type": self.doc_type,
            "subtype": self.subtype,
            "rows": self.rows,
            "fields": {
                name: {
                    "type": spec.field_type,
                    "status": spec.status,
                    "source": spec.source,
                    "coverage": spec.coverage,
                    **({"note": spec.note} if spec.note else {}),
                }
                for name, spec in self.fields.items()
            },
            "salient": list(self.salient),
            "extensions": list(self.extensions),
        }


_LIST_JSON = {"type": ["array", "null"], "items": {"type": "string"}}
_TYPE_JSON: dict[str, dict[str, Any]] = {
    "money": {"type": ["string", "number", "null"]},
}


def _json_for(field_type: str) -> dict[str, Any]:
    """JSON Schema fragment for a scorer field type."""
    if field_type.startswith("entity_list"):
        return dict(_LIST_JSON)
    return dict(_TYPE_JSON.get(field_type, {"type": ["string", "null"]}))


# ---------------------------------------------------------------------------
# Status data
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _status_data() -> Mapping[str, Any]:
    """The frozen per-subtype GT status (``data/extraction_status.json``)."""
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def extraction_status_source() -> dict[str, Any]:
    """Which dataset revision the field statuses were frozen from."""
    data = _status_data()
    return {key: data[key] for key in ("dataset", "config", "revision", "commit", "rows")}


def _merge(counters: list[Mapping[str, int]]) -> dict[str, int]:
    """Sum status counters."""
    out: dict[str, int] = {}
    for counter in counters:
        for key, value in counter.items():
            out[key] = out.get(key, 0) + int(value)
    return out


def _classify(counts: Mapping[str, int] | None, rows: int) -> tuple[str, float | None]:
    """Status and coverage of a ``gt_fields`` field from its presence counts."""
    if not rows:
        return "pending_annotation", None
    counts = counts or {}
    populated = counts.get("populated", 0) / rows
    absent = counts.get("schema_documented_absence", 0) / rows
    pending = counts.get("pending_annotation", 0) / rows
    not_applicable = counts.get("not_applicable", 0) / rows
    if populated >= _SCORED_SHARE:
        return "scored", populated
    if populated > 1 - _SCORED_SHARE:
        # Some rows label it, others document it absent (auto ``adjuster``):
        # scored where the row labels it.
        return "partial", populated
    if absent >= 0.5:
        return "documented_absent", populated
    if populated > 0:
        return "partial", populated
    if pending >= 0.5:
        return "pending_annotation", populated
    if not_applicable >= 0.5:
        return "not_applicable", populated
    return "pending_annotation", populated


def _label_status(counts: Mapping[str, int] | None, rows: int) -> tuple[str, float | None]:
    """Status of a field derived from a label source with ``counts`` coverage."""
    if not rows:
        return "derived", None
    share = (counts or {}).get("populated", 0) / rows
    return ("derived" if share >= 0.5 else "pending_annotation"), share


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _live_types(doc_type: str) -> dict[str, str]:
    """The class's live field map (``DEFAULT_FIELD_TYPES``)."""
    from ..scoring.suites import DEFAULT_FIELD_TYPES

    return dict(DEFAULT_FIELD_TYPES[doc_type])


def _subtype_overlay(doc_type: str, subtype: str | None) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Extra or retyped fields, salient guidance and extension names for a subtype."""
    types: dict[str, str] = {}
    salient: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    if subtype is None:
        return types, salient, extensions
    if doc_type == "insurance_claim":
        from ..scoring.field_scoring import CMS_ROW_FIELD_TYPES
        from .corpus import cms_source_table

        if cms_source_table(doc_type, subtype) is not None:
            types.update(CMS_ROW_FIELD_TYPES)
            extensions = ("cms_source_table",)
    elif doc_type == "contract":
        from .cuad_schema import salient_categories

        salient = tuple(salient_categories(subtype))
    elif doc_type == "merger_agreement":
        from .maud_schema import consideration_subclass

        salient = tuple(consideration_subclass(subtype).priority_questions)
    elif doc_type == "corporate_record":
        from .corporate_record_schema import entity_field_types

        entity = entity_field_types(subtype)
        extensions = tuple(name for name in entity if name not in _live_types(doc_type))
        types.update({name: entity[name] for name in extensions})
        salient = tuple(entity)
    return types, salient, extensions


def _build(doc_type: str, subtype: str | None) -> ExtractionSchema:
    """Assemble one schema from the live map, the overlay and the status data."""
    data = _status_data()
    by_subtype = data["doc_types"].get(doc_type, {})
    if subtype is None:
        parts = list(by_subtype.values())
    else:
        parts = [by_subtype[subtype]] if subtype in by_subtype else []
    rows = sum(int(part["rows"]) for part in parts)
    counts: dict[str, dict[str, int]] = {}
    for part in parts:
        for name, counter in part["fields"].items():
            counts[name] = _merge([counts.get(name, {}), counter])

    types = _live_types(doc_type)
    overlay, salient, extensions = _subtype_overlay(doc_type, subtype)
    types.update(overlay)
    if doc_type == "insurance_claim" and subtype is None:
        # The class-level schema names the CMS field too; it is derived on CMS rows only.
        types.setdefault("cms_source_table", "label")
    types.update(_LABEL_TASK_FIELDS.get(doc_type, {}))

    fields: dict[str, FieldSchema] = {}
    for name, field_type in types.items():
        if name in _FOREIGN.get(doc_type, ()):
            fields[name] = FieldSchema(name, field_type, "not_applicable", "other class",
                                       note="declared by the live map for another class")
            continue
        if name in _LABEL_TASK_FIELDS.get(doc_type, {}):
            status, coverage = _classify(counts.get(name), rows)
            fields[name] = FieldSchema(name, field_type, "label_task", "gt_fields", coverage)
            continue
        if doc_type == "contract" and name in _CUAD_DERIVED:
            status, coverage = _label_status(counts.get("cuad_clause_labels"), rows)
            fields[name] = FieldSchema(name, field_type, status, "cuad_clause_labels", coverage,
                                       note=_CUAD_DERIVED[name])
            continue
        if doc_type == "merger_agreement" and name == "maud_clauses":
            status, coverage = _label_status(counts.get("maud_clause_labels"), rows)
            fields[name] = FieldSchema(name, field_type, status, "maud_clause_labels", coverage)
            continue
        if _SUBTYPE_DERIVED.get(doc_type) == name or (
            doc_type == "insurance_claim" and name == "cms_source_table"
        ):
            fields[name] = FieldSchema(name, field_type, "derived", "subtype", 1.0 if rows else None,
                                       note="truth is the row's subtype"
                                       + (" (CMS tables only)" if name == "cms_source_table" else ""))
            continue
        status, coverage = _classify(counts.get(name), rows)
        fields[name] = FieldSchema(name, field_type, status, "gt_fields", coverage)

    return ExtractionSchema(
        doc_type=doc_type,
        subtype=subtype,
        rows=rows,
        fields=MappingProxyType(fields),
        salient=salient,
        revision=str(data.get("revision") or ""),
        extensions=extensions,
    )


def _check_doc_type(doc_type: str) -> str:
    """Reject a class outside the five mailroom classes."""
    if doc_type not in MAILROOM_DOC_TYPES:
        raise KeyError(f"unknown mailroom class {doc_type!r}; known: {list(MAILROOM_DOC_TYPES)}")
    return doc_type


def extraction_subtypes(doc_type: str) -> tuple[str, ...]:
    """Every subtype of ``doc_type``: the taxonomy catalog, plus any GT-only key."""
    _check_doc_type(doc_type)
    seen = list(DOC_TYPE_SUBCLASSES.get(doc_type, ()))
    for subtype in _status_data()["doc_types"].get(doc_type, {}):
        if subtype not in seen:
            seen.append(subtype)
    return tuple(seen)


@lru_cache(maxsize=None)
def _cached(doc_type: str, subtype: str | None) -> ExtractionSchema:
    """Memoised :func:`_build`."""
    return _build(doc_type, subtype)


def extraction_schema(doc_type: str, subtype: Any = None) -> ExtractionSchema:
    """The document-type schema, or the schema of one subtype.

    ``subtype`` accepts any corpus surface (``License_Agreements``,
    ``Mixed Cash & Stock``); it is normalised with
    :func:`~llm_dojo_scoring.schemas.corpus.normalize_corpus_subclass`.
    """
    _check_doc_type(doc_type)
    if subtype is None or (isinstance(subtype, str) and not subtype.strip()):
        return _cached(doc_type, None)
    key = normalize_corpus_subclass(doc_type, subtype)
    if key not in extraction_subtypes(doc_type):
        raise KeyError(f"unknown {doc_type} subtype {subtype!r}")
    return _cached(doc_type, key)


def iter_extraction_schemas(*, include_doc_types: bool = True) -> Iterator[ExtractionSchema]:
    """Every schema: each class's document-type schema, then each subtype."""
    for doc_type in MAILROOM_DOC_TYPES:
        if include_doc_types:
            yield extraction_schema(doc_type)
        for subtype in extraction_subtypes(doc_type):
            yield extraction_schema(doc_type, subtype)


def schema_coverage(
    doc_type: str,
    subtype: Any = None,
    expected: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Which schema fields one GT row labels, and which it leaves unscored.

    Returns the ``schema_id``, the fields the row can score (schema-scored and
    labelled, or documented-absent), the schema-scored fields the row leaves
    unlabelled, and the schema's pending fields. Additive reporting only.
    """
    from .gt_metadata import ABSENT_PRESENCE_STATUSES, gt_presence_map, is_empty_value, parse_gt_fields

    schema = extraction_schema(doc_type, subtype)
    gt = parse_gt_fields(expected) if expected is not None else {}
    presence = gt_presence_map(gt)

    def carries(key: str) -> bool:
        """The row labels ``key``: a pending / n/a / absent status overrides a stale value."""
        return presence.get(key) not in ABSENT_PRESENCE_STATUSES and not is_empty_value(gt.get(key))

    from .corpus import CMS_DOCUMENTED_ABSENT_FIELDS, cms_source_table

    # Field scoring credits a documented absence only on CMS rows (#93); the
    # table comes from the row's own key when set, else from the subtype.
    table = gt.get("cms_source_table")
    is_cms = cms_source_table(doc_type, subtype if is_empty_value(table) else table) is not None

    labelled: list[str] = []
    unlabelled: list[str] = []
    for name in schema.scored_fields:
        spec = schema.fields[name]
        absent = spec.status == "documented_absent" or presence.get(name) == "schema_documented_absence"
        if spec.source == "subtype":
            ok = True
        elif absent and is_cms and name in CMS_DOCUMENTED_ABSENT_FIELDS:
            ok = True
        elif spec.source in _LABEL_SOURCES:
            # CUAD / MAUD derived fields score from the row's label source, or a
            # value supplied directly for the field.
            ok = carries(name) or carries(spec.source)
        else:
            ok = carries(name)
        (labelled if ok else unlabelled).append(name)
    return {
        "schema_id": schema.schema_id,
        "doc_type": doc_type,
        "subtype": schema.subtype,
        "scorable_fields": labelled,
        "unlabelled_fields": unlabelled,
        "pending_fields": list(schema.pending_fields),
    }


def registry_violations() -> list[str]:
    """Drift between the registry, the taxonomy and the live field maps (empty = OK)."""
    from ..pipeline.mailroom import LIVE_DOC_TYPES

    problems: list[str] = []
    if tuple(LIVE_DOC_TYPES) != MAILROOM_DOC_TYPES:
        problems.append(f"MAILROOM_DOC_TYPES {MAILROOM_DOC_TYPES} != LIVE_DOC_TYPES {tuple(LIVE_DOC_TYPES)}")
    data = _status_data()
    if set(data["doc_types"]) != set(MAILROOM_DOC_TYPES):
        problems.append(f"status data classes {sorted(data['doc_types'])} != the five mailroom classes")
    for doc_type in MAILROOM_DOC_TYPES:
        live = _live_types(doc_type)
        base = extraction_schema(doc_type)
        missing = sorted(set(live) - set(base.fields))
        if missing:
            problems.append(f"{doc_type}: live-map fields missing from the schema: {missing}")
        for subtype in data["doc_types"].get(doc_type, {}):
            if subtype not in DOC_TYPE_SUBCLASSES.get(doc_type, ()):
                problems.append(f"{doc_type}: GT subtype {subtype!r} is not in the taxonomy catalog")
        for subtype in extraction_subtypes(doc_type):
            schema = extraction_schema(doc_type, subtype)
            for name, spec in schema.fields.items():
                if spec.status not in FIELD_STATUSES:
                    problems.append(f"{schema.schema_id}.{name}: unknown status {spec.status!r}")
            lost = sorted(set(live) - set(schema.fields))
            if lost:
                problems.append(f"{schema.schema_id}: drops live-map fields {lost}")
    return problems
