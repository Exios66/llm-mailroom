"""Staged ground-truth overlay: interim labels the published Hub merge does not carry yet.

The Hub ``ground_truth`` rows leave key fields unlabelled: corporate entity, jurisdiction,
dates and signatories; correspondence sender, recipient and date; every merger field except
consideration. A few others are labels the document contradicts. Until a dataset revision
publishes them, an **interim overlay** can supply them. The overlay is built outside this
repo by the local-mailroom-sandbox (``scripts/interim_labels.py build``) from extractions its
extraction gate accepted field by field, plus labels derived from the row itself.

This module is the pipeline's loader for that overlay. It is **off by default**: nothing is
read unless a caller passes an overlay, or both ``MAILROOM_INTERIM_GT_DIR`` and
``MAILROOM_INTERIM_GT_SHA256`` are set.

**Pinned file, not vendored data.** The overlay stays where its builder writes it, and the
caller pins the sha256 of its ``MANIFEST.json``, as ``llm/frozen_v1/lineage.json`` pins
prompts by content. The manifest pins every class file's sha256 in turn. A wrong pin, an
edited file or an unlisted file raises :class:`InterimOverlayError`; the loader never falls
back to Hub-only gold quietly.

Every label carries a ``status``:

``gap_fill``    the Hub has no label for the field; the value is a gate-accepted, grounded
                extraction.
``derived``     computed from the row, with no model involved (``cms_source_table`` and
                ``record_type`` / ``communication_type`` from ``expected_subclass``). The
                catalog already derives ``cms_source_table`` from the Hub subclass (#283),
                so such a label is superseded rather than applied.
``correction``  a documented ground-truth or scorer defect makes the Hub label unattainable.
``disputed``    the Hub and a gate-supported extraction disagree and no rule decides. This is
                **not** truth; it is never applied and is kept for human review.

Merge rules (:func:`apply_interim_labels`):

* **A label the Hub publishes supersedes the overlay.**
  * A ``gap_fill`` or ``derived`` label applies only where the Hub-built gold has no value.
  * A ``correction`` applies only while the Hub value still equals the ``hub_value`` the
    correction was made against. A Hub revision that publishes a different value wins.
* A label binds to the exact text it was read from (``doc_sha256``). A document whose text
  changed is left untouched.
* A label on a closed inventory field (``record_type``, ``communication_type``,
  ``claim_type``, ``cms_source_table``) must be a value of the specialist's vocabulary. The
  sandbox overlay derives ``record_type`` from the raw subclass, so ``board_resolution`` or
  ``indenture`` would become gold that no extraction can emit. The mailroom gold leaves
  those rows unlabelled on purpose (``catalog_expected_fields``), so such labels are rejected.
* **CMS ``claim_type`` is ``health``.** Since llm-mailroom #280 the product line of every
  CMS/DE-SynPUF row is ``health`` and the table is its own field (``cms_source_table``). An
  overlay label that would put anything else on a CMS row's ``claim_type`` is rejected. Older
  overlays called the ``health`` label a defect; it is the correct gold.
* Applied fields are tagged ``interim:<status>`` in ``expected_fields_sources``.
  ``labelled_expected_fields`` scores them as gold. The overlay's manifest sha256 is recorded
  in the GT meta (``interim_gt``), so a score that used the overlay is never mistaken for a
  score on Hub ground truth.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "mailroom-interim-labels/v1"
STATUSES = ("gap_fill", "derived", "correction", "disputed")
TRUTH_STATUSES = ("gap_fill", "derived", "correction")
SOURCE_PREFIX = "interim:"
ENV_DIR = "MAILROOM_INTERIM_GT_DIR"
ENV_SHA256 = "MAILROOM_INTERIM_GT_SHA256"
CMS_TABLES = frozenset({"pde", "inpatient", "outpatient", "carrier"})


class InterimOverlayError(RuntimeError):
    """The overlay does not match its pin, or its files do not match its manifest."""


@dataclass(frozen=True)
class InterimOverlay:
    """A verified overlay: ``records`` maps ``doc_id`` to its record."""

    manifest_sha256: str
    root: str
    records: Mapping[str, Mapping[str, Any]] = field(repr=False)

    def provenance(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "overlay_sha256": self.manifest_sha256, "documents": len(self.records)}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_sha(doc_text: str | None) -> str:
    """Short hash binding a label to the document text it was read from (builder's format)."""
    return _sha256(doc_text or "")[:16]


def load_interim_overlay(root: str | Path, *, sha256: str) -> InterimOverlay:
    """Load and verify the overlay at ``root`` against the pinned manifest ``sha256``."""
    base = Path(root)
    manifest_path = base / "MANIFEST.json"
    if not manifest_path.is_file():
        raise InterimOverlayError(f"interim overlay: {manifest_path} not found")
    text = manifest_path.read_text(encoding="utf-8")
    actual = _sha256(text)
    if actual != str(sha256 or "").strip().lower():
        raise InterimOverlayError(
            f"interim overlay: MANIFEST.json sha256 {actual} does not match the pin {sha256!r}"
        )
    manifest = json.loads(text)
    if manifest.get("schema") != SCHEMA:
        raise InterimOverlayError(f"interim overlay: schema {manifest.get('schema')!r} is not {SCHEMA!r}")
    files = manifest.get("files") or {}
    if not files:
        raise InterimOverlayError("interim overlay: the manifest lists no files")
    unlisted = sorted(p.name for p in base.glob("*.jsonl") if p.name not in files)
    if unlisted:
        raise InterimOverlayError(f"interim overlay: files not in the manifest: {', '.join(unlisted)}")
    records: dict[str, Mapping[str, Any]] = {}
    for name, pinned in sorted(files.items()):
        path = base / name
        if not path.is_file():
            raise InterimOverlayError(f"interim overlay: {name} is listed in the manifest but missing")
        body = path.read_text(encoding="utf-8")
        if _sha256(body) != pinned:
            raise InterimOverlayError(f"interim overlay: {name} sha256 differs from the manifest")
        for line in body.splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            for fname, lab in (rec.get("labels") or {}).items():
                status = lab.get("status")
                if status not in STATUSES:
                    raise InterimOverlayError(f"interim overlay: {rec.get('doc_id')}.{fname}: unknown status {status!r}")
                if bool(lab.get("use_as_truth")) != (status in TRUTH_STATUSES):
                    raise InterimOverlayError(
                        f"interim overlay: {rec.get('doc_id')}.{fname}: use_as_truth does not follow status {status!r}"
                    )
            records[str(rec["doc_id"])] = rec
    return InterimOverlay(manifest_sha256=actual, root=str(base), records=records)


_ACTIVE: dict[tuple[str, str], InterimOverlay] = {}


def active_overlay() -> InterimOverlay | None:
    """The env-configured overlay, or ``None`` (the default).

    Both ``MAILROOM_INTERIM_GT_DIR`` and ``MAILROOM_INTERIM_GT_SHA256`` must be set; one
    without the other is a configuration error, not a silent Hub-only run.
    """
    root = os.environ.get(ENV_DIR, "").strip()
    pin = os.environ.get(ENV_SHA256, "").strip()
    if not root and not pin:
        return None
    if not root or not pin:
        raise InterimOverlayError(f"interim overlay: set both {ENV_DIR} and {ENV_SHA256}, or neither")
    key = (root, pin.lower())
    if key not in _ACTIVE:
        _ACTIVE[key] = load_interim_overlay(root, sha256=pin)
    return _ACTIVE[key]


def _empty(value: Any) -> bool:
    return value in (None, "", [], {}) or (
        isinstance(value, str) and value.strip().lower() in {"nan", "none", "null", "n/a"}
    )


def _canon(value: Any) -> str:
    from langchain_agents.doc_inventories import coerce_gt_value

    value = coerce_gt_value(value)
    return "" if _empty(value) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _closed_vocabulary(name: str) -> tuple[str, ...] | None:
    """The closed label set the specialist schema allows for an inventory field, else ``None``."""
    from langchain_agents import doc_inventories as inv

    return {
        "record_type": inv.CORPORATE_RECORD_TYPES,
        "communication_type": inv.CORRESPONDENCE_TYPES,
        "claim_type": inv.INSURANCE_PRODUCT_LINES,
        "cms_source_table": tuple(sorted(CMS_TABLES)),
    }.get(name)


def _cms_row(doc_class: str, record: Mapping[str, Any], fields: Mapping[str, Any]) -> bool:
    sub = str(record.get("doc_subclass") or "").strip().lower()
    return doc_class == "insurance_claim" and (sub in CMS_TABLES or str(fields.get("cms_source_table") or "") in CMS_TABLES)


def apply_interim_labels(
    fields: Mapping[str, Any],
    sources: Mapping[str, str],
    sample: Mapping[str, Any],
    overlay: InterimOverlay,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Merge one document's truth labels into Hub-built gold. Returns ``(fields, sources, report)``.

    ``report`` lists ``applied`` (field -> status), ``superseded`` (Hub won), ``rejected``
    (failed a merge rule), ``disputed`` (never applied) and ``absent`` (a null truth label,
    which the mailroom gold cannot express). It always carries the overlay sha256.
    """
    out_fields = dict(fields)
    out_sources = dict(sources)
    report: dict[str, Any] = {**overlay.provenance(), "applied": {}, "superseded": [], "rejected": [], "disputed": [], "absent": []}
    doc_id = str(sample.get("hub_id") or sample.get("id") or "")
    record = overlay.records.get(doc_id) if doc_id else None
    if not record:
        return out_fields, out_sources, report
    text = sample.get("text") or sample.get("doc_text") or ""
    if record.get("doc_sha256") != doc_sha(text):
        report["rejected"].append("*: document text differs from the labelled text")
        return out_fields, out_sources, report
    doc_class = str(sample.get("expected_hf_class") or sample.get("expected") or record.get("doc_class") or "")
    if record.get("doc_class") and doc_class and record["doc_class"] != doc_class:
        report["rejected"].append(f"*: overlay class {record['doc_class']} is not {doc_class}")
        return out_fields, out_sources, report
    for name, lab in sorted((record.get("labels") or {}).items()):
        status = lab.get("status")
        if status == "disputed" or not lab.get("use_as_truth"):
            report["disputed"].append(name)
            continue
        value = lab.get("value")
        if name == "claim_type" and _cms_row(doc_class, record, out_fields) and str(value or "").strip().lower() != "health":
            # #280: a CMS row's claim_type is the product line, health. Not a defect to correct.
            report["rejected"].append(f"{name}: a CMS row's claim_type is health")
            continue
        hub_value = out_fields.get(name)
        if status == "correction":
            if _canon(hub_value) != _canon(lab.get("hub_value")):
                report["superseded"].append(name)
                continue
        elif not _empty(hub_value):
            report["superseded"].append(name)
            continue
        if _empty(value):
            # A null truth label documents absence. The mailroom gold has no absence marker
            # (an empty value is unlabelled), so it is reported, not applied.
            report["absent"].append(name)
            continue
        vocabulary = _closed_vocabulary(name)
        if vocabulary is not None and value not in vocabulary:
            # e.g. a raw ``board_resolution`` subclass as record_type: no extraction can emit it.
            report["rejected"].append(f"{name}: {value!r} is not in the closed {name} vocabulary")
            continue
        out_fields[name] = value
        out_sources[name] = SOURCE_PREFIX + str(status)
        report["applied"][name] = status
    return out_fields, out_sources, report


def is_interim_source(source: Any) -> bool:
    return isinstance(source, str) and source.startswith(SOURCE_PREFIX)
