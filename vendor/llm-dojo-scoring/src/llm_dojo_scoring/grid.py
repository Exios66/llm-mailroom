"""Specialist grid reports for local → Modal GPU experiment studies.

Aligned with the **L4 Specialist Grid** style of results summary:

* one row per specialist (optionally compressed across experiments) with the
  class metric, ``ok / n``, p50 latency, and GPU cost **per ok document**;
* a pooled serving-efficiency table per experiment (error rate,
  documents/minute, tokens/second/GPU, GPU cost/document);
* a cost table separating **busy-window** GPU time (the cells' own GPU time)
  from the **metered** provider session (cold boots, gates, warm idle,
  teardown) — the L4 grid's ``busy share`` / ``metered per document``.

Honesty rules (same as the rest of the package):

* ``serving_kind`` stays ``local`` / ``modal`` / ``api`` — Modal is never
  remapped to local.
* Every busy-window number is labeled ``cost_basis="busy_window"``; metered
  numbers are ``billed_incl_cold``. A missing input renders ``n/a`` — it is
  never fabricated (no metered report ⇒ no metered per-document estimate).
* A specialist row must carry a single ``metric_id``; mixing metric ids in
  one row raises (issue #17). Do not compare CUAD micro-F1 with MAUD accuracy
  in one cell.
* ``ok`` counts come from the per-document records; error rate is derived
  from explicit ``errored`` / ``error`` / ``error_class`` fields via
  :func:`~llm_dojo_scoring.scorecard_honesty.summarize_run_completion`.

This module computes **nothing new**; it groups the per-document scores,
latencies, and GPU seconds the pipeline already records.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from .cost import resolve_cost_basis
from .scorecard_honesty import (
    COST_BASIS_VALUES,
    canonical_error_class,
    normalize_serving_kind_token,
    summarize_run_completion,
)

__all__ = [
    "GridDocument",
    "GridExperiment",
    "specialist_grid_rows",
    "serving_efficiency_rows",
    "session_cost_rows",
    "grid_scorecard",
    "build_grid_report",
]

_SERVING_KINDS = frozenset({"local", "modal", "api"})


@dataclass(frozen=True)
class GridDocument:
    """One scored document in one cell of the grid."""

    experiment: str
    specialist: str
    metric_id: str | None = None
    score: float | None = None
    coverage: float | None = None
    ok: bool | None = None
    latency_seconds: float | None = None
    #: GPU seconds attributed to this document (busy window, not cold boot).
    gpu_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    error: str | None = None
    error_class: str | None = None
    #: ``"unscorable"`` when the document has no extractable ground truth
    #: (#16): it counts toward ``n`` / errors / cost but never toward quality.
    status: str | None = None


@dataclass(frozen=True)
class GridExperiment:
    """One experiment posture (hardware + client + session accounting)."""

    name: str
    gpus: int | None = None
    gpu_type: str = ""
    gpu_hourly_usd: float | None = None
    client_concurrency: int | None = None
    #: Busy wall-clock seconds for the experiment (cells' own run window).
    wall_seconds: float | None = None
    #: Whole provider session from the billing report (Modal etc.).
    metered_usd: float | None = None
    billed_usd: float | None = None
    serving_kind: str = "modal"
    documents_per_class: int | None = None
    status: str = ""
    posture: str = ""
    notes: str = ""


def _num(value: Any) -> float | None:
    """Coerce a number; return ``None`` for booleans, nulls, or invalid values.

    Conversion ``TypeError`` and ``ValueError`` become ``None``; other
    conversion errors propagate.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    """Coerce an integer, truncating fractional numeric values toward zero.

    Return ``None`` for booleans, nulls, or conversion ``TypeError`` /
    ``ValueError``; ``OverflowError`` (for example, infinity) propagates.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _doc(value: GridDocument | Mapping[str, Any]) -> GridDocument:
    """Normalize a document mapping, or return an existing record unchanged.

    ``doc_class`` is a fallback for a missing or empty ``specialist``.
    Only boolean ``ok`` values are retained. Numeric conversion failures
    follow :func:`_num` and :func:`_int`. Raise ``TypeError`` for other
    input types.
    """
    if isinstance(value, GridDocument):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"grid document must be GridDocument or mapping, got {type(value)!r}")
    return GridDocument(
        experiment=str(value.get("experiment") or ""),
        specialist=str(value.get("specialist") or value.get("doc_class") or ""),
        metric_id=value.get("metric_id"),
        score=_num(value.get("score")),
        coverage=_num(value.get("coverage")),
        ok=value.get("ok") if isinstance(value.get("ok"), bool) else None,
        latency_seconds=_num(value.get("latency_seconds")),
        gpu_seconds=_num(value.get("gpu_seconds")),
        prompt_tokens=_int(value.get("prompt_tokens")),
        completion_tokens=_int(value.get("completion_tokens")),
        total_tokens=_int(value.get("total_tokens")),
        error=value.get("error"),
        error_class=value.get("error_class"),
        status=_doc_status(value),
    )


def _doc_status(value: Mapping[str, Any]) -> str | None:
    """Resolve normalized document status, giving unscorable precedence over completion."""
    scores = value.get("scores")
    found = [value.get("status")]
    if isinstance(scores, Mapping):
        found.append(scores.get("status"))
    statuses = [str(s).strip().lower() for s in found if s]
    # A run-level "completed" must not hide a score-level "unscorable" (#16).
    if "unscorable" in statuses:
        return "unscorable"
    return statuses[0] if statuses else None


def _is_unscorable(doc: GridDocument) -> bool:
    """Return whether the document is explicitly marked unscorable."""
    return doc.status == "unscorable"


def _experiment(value: GridExperiment | Mapping[str, Any]) -> GridExperiment:
    """Normalize experiment fields and serving aliases without mutating input.

    Mappings use ``experiment`` as a fallback name and default an empty
    serving kind to ``modal``. Raise ``TypeError`` for unsupported input
    types and ``ValueError`` for kinds outside local, Modal, and API
    serving. Numeric conversion errors follow :func:`_num` and :func:`_int`.
    """
    if isinstance(value, GridExperiment):
        kind = normalize_serving_kind_token(value.serving_kind)
        if kind not in _SERVING_KINDS:
            raise ValueError(
                f"serving_kind must be one of {sorted(_SERVING_KINDS)}, got {value.serving_kind!r}"
            )
        return replace(value, serving_kind=kind) if kind != value.serving_kind else value
    if not isinstance(value, Mapping):
        raise TypeError(
            f"grid experiment must be GridExperiment or mapping, got {type(value)!r}"
        )
    kind = normalize_serving_kind_token(str(value.get("serving_kind") or "modal"))
    if kind not in _SERVING_KINDS:
        raise ValueError(
            f"serving_kind must be one of {sorted(_SERVING_KINDS)}, got {kind!r}"
        )
    return GridExperiment(
        name=str(value.get("name") or value.get("experiment") or ""),
        gpus=_int(value.get("gpus")),
        gpu_type=str(value.get("gpu_type") or ""),
        gpu_hourly_usd=_num(value.get("gpu_hourly_usd")),
        client_concurrency=_int(value.get("client_concurrency")),
        wall_seconds=_num(value.get("wall_seconds")),
        metered_usd=_num(value.get("metered_usd")),
        billed_usd=_num(value.get("billed_usd")),
        serving_kind=kind,
        documents_per_class=_int(value.get("documents_per_class")),
        status=str(value.get("status") or ""),
        posture=str(value.get("posture") or ""),
        notes=str(value.get("notes") or ""),
    )


def _percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolation percentile (``p`` in ``[0, 1]``).

    Return ``None`` for no values; otherwise round to six decimal places.
    """
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 6)
    rank = p * (len(vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return round(vals[lo] + (vals[hi] - vals[lo]) * frac, 6)


#: Tokens that mark a completed / no-error outcome (parity with
#: ``scorecard_honesty.summarize_run_completion``).
_SUCCESS_TOKENS = frozenset(
    {"", "stop", "completed", "success", "none", "end_turn", "eos",
     "stop_sequence", "tool_calls"}
)


def _is_error(doc: GridDocument) -> bool:
    """Return whether either error field holds a non-success token.

    Each field is checked independently so a success token in ``error``
    cannot mask a failure in ``error_class`` (and vice versa). ``ok`` is
    not consulted.
    """
    for raw in (doc.error, doc.error_class):
        if raw and str(raw).strip().lower() not in _SUCCESS_TOKENS:
            return True
    return False


def _gpu_cost_usd(docs: Iterable[GridDocument], gpu_hourly_usd: float | None) -> float | None:
    """Price available document GPU seconds at USD per GPU-hour.

    Return the summed cost rounded to six decimals, or ``None`` without a
    price or any GPU time. Missing times are omitted; GPU count is not
    applied again.
    """
    if gpu_hourly_usd is None:
        return None
    seconds = 0.0
    seen = False
    for doc in docs:
        if doc.gpu_seconds is not None:
            seen = True
            seconds += float(doc.gpu_seconds)
    if not seen:
        return None
    return round(seconds * float(gpu_hourly_usd) / 3600.0, 6)


def specialist_grid_rows(
    documents: Iterable[GridDocument | Mapping[str, Any]],
    experiments: Iterable[GridExperiment | Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Per experiment × specialist quality/cost rows (busy-window basis).

    ``score`` is the mean over scored documents; ``coverage`` the mean over
    documents that carry one. Documents with ``status="unscorable"`` (no
    ground truth, #16) are left out of both and counted in
    ``n_unscorable``; a row with only unscorable documents has
    ``quality_status="unproven"`` and ``score=None``. ``p50_latency_seconds`` uses per-document
    latencies. ``cost_per_ok_document`` is busy-window GPU cost / ok count
    (``None`` when no GPU seconds, no price, or no ok document). Available
    GPU seconds from all documents, including errors, contribute to cost.
    ``ok`` counts explicit ``True`` values, independently of errors; it is
    ``None`` when both that count and the error count are zero.

    Raise ``ValueError`` for mixed nonempty metric IDs within a group or an
    invalid experiment serving kind, and ``TypeError`` for unsupported
    record types. Numeric conversion errors not handled by :func:`_num`
    or :func:`_int` propagate.
    """
    exps = {e.name: e for e in (_experiment(x) for x in experiments)}
    docs = [_doc(d) for d in documents]
    # Materialize experiments once so the iterable is not exhausted before the
    # ordering pass below.
    exp_order = list(exps)
    groups: dict[tuple[str, str], list[GridDocument]] = {}
    for doc in docs:
        groups.setdefault((doc.experiment, doc.specialist), []).append(doc)

    rows: list[dict[str, Any]] = []
    for (exp_name, specialist), group in groups.items():
        metric_ids = sorted({d.metric_id for d in group if d.metric_id})
        if len(metric_ids) > 1:
            raise ValueError(
                f"metric_id mixed in one grid row ({exp_name} / {specialist}): "
                + ", ".join(metric_ids)
            )
        scorable = [d for d in group if not _is_unscorable(d)]
        n_unscorable = len(group) - len(scorable)
        scores = [d.score for d in scorable if d.score is not None]
        coverages = [d.coverage for d in scorable if d.coverage is not None]
        latencies = [d.latency_seconds for d in group if d.latency_seconds is not None]
        ok = sum(1 for d in group if d.ok is True)
        errored = sum(1 for d in group if _is_error(d))
        exp = exps.get(exp_name)
        price = exp.gpu_hourly_usd if exp else None
        gpu_cost = _gpu_cost_usd(group, price)
        row: dict[str, Any] = {
            "experiment": exp_name,
            "specialist": specialist,
            "metric_id": metric_ids[0] if metric_ids else None,
            "n": len(group),
            "n_scored": len(scores),
            "n_unscorable": n_unscorable,
            "quality_status": "unproven" if not scores and n_unscorable else "scored",
            "ok": ok if ok or errored else None,
            "errored": errored,
            "error_rate": round(errored / len(group), 6) if group else None,
            "score": round(sum(scores) / len(scores), 6) if scores else None,
            "coverage": round(sum(coverages) / len(coverages), 6) if coverages else None,
            "p50_latency_seconds": _percentile(latencies, 0.50),
            "gpu_cost_usd": gpu_cost,
            "cost_per_ok_document": (
                round(gpu_cost / ok, 8) if gpu_cost is not None and ok else None
            ),
            "cost_basis": "busy_window",
            "serving_kind": exp.serving_kind if exp else None,
            "gpus": exp.gpus if exp else None,
            "gpu_type": exp.gpu_type if exp else "",
        }
        rows.append(row)
    order = {name: i for i, name in enumerate(exp_order)}
    rows.sort(key=lambda r: (order.get(r["experiment"], 10_000), r["specialist"]))
    return rows


def serving_efficiency_rows(
    documents: Iterable[GridDocument | Mapping[str, Any]],
    experiments: Iterable[GridExperiment | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pooled serving efficiency per experiment (the L4 efficiency table).

    ``documents_per_minute`` needs the experiment's busy ``wall_seconds``;
    ``tokens_per_second_per_gpu`` uses completion tokens (falling back to
    total) over ``wall_seconds × gpus``. Missing token counts contribute
    zero; a zero token total or nonpositive wall time / GPU count yields
    ``None`` for token throughput. Available GPU seconds determine cost,
    divided by all documents, including errors. Emit a row for each supplied
    experiment, even when no documents match it.

    Raise ``TypeError`` for unsupported record types and ``ValueError`` for
    an invalid serving kind. Unhandled numeric conversion errors propagate.
    """
    docs = [_doc(d) for d in documents]
    out: list[dict[str, Any]] = []
    for raw in experiments:
        exp = _experiment(raw)
        group = [d for d in docs if d.experiment == exp.name]
        completion = sum(
            (d.completion_tokens if d.completion_tokens is not None else (d.total_tokens or 0))
            for d in group
        )
        errored = sum(1 for d in group if _is_error(d))
        gpu_cost = _gpu_cost_usd(group, exp.gpu_hourly_usd)
        wall = exp.wall_seconds
        gpus = exp.gpus
        out.append(
            {
                "experiment": exp.name,
                "n_documents": len(group),
                "error_rate": round(errored / len(group), 6) if group else None,
                "documents_per_minute": (
                    round(len(group) / (wall / 60.0), 6)
                    if wall and wall > 0 and group
                    else None
                ),
                "tokens_per_second_per_gpu": (
                    round(completion / wall / gpus, 6)
                    if wall and wall > 0 and gpus and gpus > 0 and completion
                    else None
                ),
                "gpu_cost_per_document": (
                    round(gpu_cost / len(group), 8)
                    if gpu_cost is not None and group
                    else None
                ),
                "gpu_cost_usd": gpu_cost,
                "cost_basis": "busy_window",
                "serving_kind": exp.serving_kind,
                "gpus": gpus,
                "gpu_type": exp.gpu_type,
            }
        )
    return out


def session_cost_rows(
    experiments: Iterable[GridExperiment | Mapping[str, Any]],
    documents: Iterable[GridDocument | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Busy-window vs metered session cost (the L4 cost table).

    ``busy_gpu_usd`` is recomputed from per-document GPU seconds; the metered
    session is only what the billing report provides — never an estimate.
    Costs are in USD. ``busy_share`` is a fraction, or ``None`` if busy cost
    is missing or metered cost is missing or nonpositive. Per-document cost
    divides metered cost by all matching documents; no documents yields
    ``None``. ``billed_usd`` is passed through separately.

    Raise ``TypeError`` for unsupported record types and ``ValueError`` for
    an invalid serving kind. Unhandled numeric conversion errors propagate.
    """
    docs = [_doc(d) for d in documents]
    out: list[dict[str, Any]] = []
    for raw in experiments:
        exp = _experiment(raw)
        group = [d for d in docs if d.experiment == exp.name]
        busy = _gpu_cost_usd(group, exp.gpu_hourly_usd)
        metered = exp.metered_usd
        share = (
            round(busy / metered, 6)
            if busy is not None and metered is not None and metered > 0
            else None
        )
        out.append(
            {
                "session": exp.name,
                "documents": len(group),
                "busy_gpu_usd": busy,
                "metered_usd": metered,
                "busy_share": share,
                "metered_per_document": (
                    round(metered / len(group), 8)
                    if metered is not None and group
                    else None
                ),
                "billed_usd": exp.billed_usd,
                "serving_kind": exp.serving_kind,
                "cost_basis": "billed_incl_cold",
            }
        )
    return out


def grid_scorecard(
    *,
    documents: Iterable[GridDocument | Mapping[str, Any]],
    experiments: Iterable[GridExperiment | Mapping[str, Any]],
    title: str = "Specialist Grid: Results and Cost Summary",
    setup: str = "",
    findings: Sequence[str] = (),
    provenance: Mapping[str, Any] | None = None,
    figures: Mapping[str, str] | None = None,
    appendix_url: str | None = None,
) -> dict[str, Any]:
    """Structured scorecard (JSON-friendly) for the grid report.

    Include experiment metadata, specialist quality, serving efficiency,
    session costs, and copies of the supplied report annotations. ``figures``
    maps captions to image paths or URLs; no figures are loaded or written.

    Propagate record validation and numeric conversion errors from the row
    builders, including ``ValueError`` for mixed metric IDs. Also raise
    ``ValueError`` for an invalid nonempty provenance serving kind.
    """
    exp_list = [_experiment(e) for e in experiments]
    doc_list = [_doc(d) for d in documents]
    specialists = specialist_grid_rows(doc_list, exp_list)
    efficiency = serving_efficiency_rows(doc_list, exp_list)
    cost = session_cost_rows(exp_list, doc_list)
    bases = {row["cost_basis"] for row in cost}
    # busy rows are labeled busy_window, metered billed_incl_cold; both are
    # explicit labels, so this never mixes unlabeled rows. Validate anyway.
    resolve_cost_basis([{"cost_basis": b} for b in sorted(bases)])
    if provenance is not None:
        kind = normalize_serving_kind_token(str(provenance.get("serving_kind") or ""))
        if kind and kind not in _SERVING_KINDS:
            raise ValueError(f"provenance serving_kind invalid: {kind!r}")
    return {
        "title": title,
        "setup": setup,
        "findings": list(findings),
        "provenance": dict(provenance or {}),
        "experiments": [
            {
                "experiment": e.name,
                "posture": e.posture,
                "gpus": e.gpus,
                "gpu_type": e.gpu_type,
                "client_concurrency": e.client_concurrency,
                "documents_per_class": e.documents_per_class,
                "status": e.status,
                "serving_kind": e.serving_kind,
            }
            for e in exp_list
        ],
        "specialists": specialists,
        "serving_efficiency": efficiency,
        "cost": cost,
        "figures": dict(figures or {}),
        "appendix_url": appendix_url,
    }


def _fmt_usd(value: float | None) -> str:
    """Format USD with five decimals below $1 in magnitude, otherwise two.

    Return ``"n/a"`` for ``None``.
    """
    if value is None:
        return "n/a"
    return f"${value:.5f}" if abs(value) < 1 else f"${value:.2f}"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    """Format a number to ``digits`` decimal places, or ``"n/a"`` for ``None``."""
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    """Format a fraction as a percentage, or ``"n/a"`` for ``None``."""
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _fmt_score(row: Mapping[str, Any]) -> str:
    """Format a score to three decimals with optional coverage as a percentage.

    Return ``"n/a"`` when the score is absent, even if coverage is present.
    """
    score = row.get("score")
    if score is None:
        return "unproven" if row.get("quality_status") == "unproven" else "n/a"
    text = f"{float(score):.3f}"
    if row.get("coverage") is not None:
        text += f" ({float(row['coverage']) * 100:.0f}%)"
    return text


def _fmt_ok(row: Mapping[str, Any]) -> str:
    """Format the ok count over n, using ``n/a`` when ok and errors are absent."""
    if row.get("ok") is None and not row.get("errored"):
        return f"n/a / {row.get('n')}"
    return f"{row.get('ok') or 0}/{row.get('n')}"


def _fmt_busy_share(value: float | None) -> str:
    """Format a fractional busy share as a whole percentage, or ``"n/a"``."""
    if value is None:
        return "n/a"
    return f"{value * 100:.0f}%"


def _md(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """Return Markdown table lines without escaping cell text."""
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _compact_specialist_rows(rows: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """One row per specialist, cells joined with ``·`` across experiments."""
    order: list[str] = []
    by_specialist: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        spec = str(row["specialist"])
        if spec not in by_specialist:
            by_specialist[spec] = []
            order.append(spec)
        by_specialist[spec].append(row)
    out: list[list[str]] = []
    for spec in order:
        group = by_specialist[spec]
        out.append(
            [
                spec,
                " \u00b7 ".join(_fmt_score(r) for r in group),
                " \u00b7 ".join(_fmt_ok(r) for r in group),
                " \u00b7 ".join(_fmt_num(r["p50_latency_seconds"], 1) for r in group),
                " \u00b7 ".join(_fmt_usd(r["cost_per_ok_document"]) for r in group),
            ]
        )
    return out


def build_grid_report(
    *,
    documents: Iterable[GridDocument | Mapping[str, Any]],
    experiments: Iterable[GridExperiment | Mapping[str, Any]],
    title: str = "Specialist Grid: Results and Cost Summary",
    setup: str = "",
    findings: Sequence[str] = (),
    provenance: Mapping[str, Any] | None = None,
    figures: Mapping[str, str] | None = None,
    appendix_url: str | None = None,
    compact: bool = True,
) -> str:
    """Render the L4-grid-style Markdown report.

    Tables: experiment posture, pooled serving efficiency, quality/cost by
    specialist, and busy-window vs metered session cost. ``compact=True``
    joins each specialist's cells across experiments with ``·`` (the L4
    layout); ``compact=False`` emits one row per experiment × specialist.
    Missing values render as ``n/a``; each total cost requires that cost for
    every session. ``figures`` maps captions to image paths or URLs, which
    are embedded as links. Return the report text without writing files.
    Validation and conversion errors from :func:`grid_scorecard` propagate.
    """
    card = grid_scorecard(
        documents=documents,
        experiments=experiments,
        title=title,
        setup=setup,
        findings=findings,
        provenance=provenance,
        figures=figures,
        appendix_url=appendix_url,
    )
    lines: list[str] = [f"# {card['title']}", ""]

    if card["findings"]:
        lines.append("## Key findings")
        lines.append("")
        for i, item in enumerate(card["findings"], start=1):
            lines.append(f"{i}. {item}")
        lines.append("")

    if card["setup"]:
        lines.append(f"**Setup:** {card['setup']}")
        lines.append("")

    if card["experiments"]:
        lines.append("| Experiment | Posture | GPUs | Client concurrency | Documents per class | Status |")
        lines.append("| --- | --- | ---: | ---: | ---: | --- |")
        for e in card["experiments"]:
            gpus = "n/a" if e["gpus"] is None else str(e["gpus"])
            if e["gpu_type"]:
                gpus = f"{gpus}×{e['gpu_type']}"
            conc = "n/a" if e["client_concurrency"] is None else str(e["client_concurrency"])
            docs_n = "n/a" if e["documents_per_class"] is None else str(e["documents_per_class"])
            lines.append(
                f"| {e['experiment']} | {e['posture'] or 'n/a'} | {gpus} | {conc} | "
                f"{docs_n} | {e['status'] or 'n/a'} |"
            )
        lines.append("")

    if card["serving_efficiency"]:
        lines.append("## Serving efficiency (pooled across the specialists)")
        lines.append("")
        eff = card["serving_efficiency"]
        names = [e["experiment"] for e in eff]
        lines.append("| Metric | " + " | ".join(names) + " |")
        lines.append("| --- |" + "|".join(["---:"] * len(names)) + "|")
        metric_rows = [
            ("Error rate", lambda e: _fmt_pct(e["error_rate"])),
            ("Documents per minute", lambda e: _fmt_num(e["documents_per_minute"])),
            (
                "Tokens per second per GPU",
                lambda e: _fmt_num(e["tokens_per_second_per_gpu"], 0),
            ),
            ("GPU cost per document", lambda e: _fmt_usd(e["gpu_cost_per_document"])),
        ]
        for label, fn in metric_rows:
            lines.append("| " + label + " | " + " | ".join(fn(e) for e in eff) + " |")
        lines.append("")

    if card["specialists"]:
        lines.append("## Quality and cost by specialist")
        lines.append("")
        lines.append(
            "| Specialist | Score | ok / n | p50 latency (s) | $ per ok document |"
        )
        lines.append("| --- | :---: | :---: | :---: | :---: |")
        if compact:
            rows = _compact_specialist_rows(card["specialists"])
        else:
            rows = [
                [
                    f"{r['specialist']} ({r['experiment']})",
                    _fmt_score(r),
                    _fmt_ok(r),
                    _fmt_num(r["p50_latency_seconds"], 1),
                    _fmt_usd(r["cost_per_ok_document"]),
                ]
                for r in card["specialists"]
            ]
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    if card["cost"]:
        lines.append("## Cost")
        lines.append("")
        lines.append(
            "| Session | Documents | Busy-window GPU | Metered session | Busy share | "
            "Metered per document | Billed |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in card["cost"]:
            lines.append(
                "| {session} | {documents} | {busy} | {metered} | {share} | {per_doc} | {billed} |".format(
                    session=row["session"],
                    documents=row["documents"],
                    busy=_fmt_usd(row["busy_gpu_usd"]),
                    metered=_fmt_usd(row["metered_usd"]),
                    share=_fmt_busy_share(row["busy_share"]),
                    per_doc=_fmt_usd(row["metered_per_document"]),
                    billed="n/a" if row["billed_usd"] is None else _fmt_usd(row["billed_usd"]),
                )
            )
        total_docs = sum(r["documents"] for r in card["cost"])
        total_busy = (
            sum(r["busy_gpu_usd"] for r in card["cost"])
            if all(r["busy_gpu_usd"] is not None for r in card["cost"])
            else None
        )
        total_metered = (
            sum(r["metered_usd"] for r in card["cost"])
            if all(r["metered_usd"] is not None for r in card["cost"])
            else None
        )
        total_share = (
            round(total_busy / total_metered, 6)
            if total_busy is not None and total_metered
            else None
        )
        total_per_doc = (
            round(total_metered / total_docs, 8)
            if total_metered is not None and total_docs
            else None
        )
        total_billed = (
            sum(r["billed_usd"] for r in card["cost"])
            if all(r["billed_usd"] is not None for r in card["cost"])
            else None
        )
        lines.append(
            "| **Total** | {docs} | {busy} | {metered} | {share} | {per_doc} | {billed} |".format(
                docs=total_docs,
                busy=_fmt_usd(total_busy),
                metered=_fmt_usd(total_metered),
                share=_fmt_busy_share(total_share),
                per_doc=_fmt_usd(total_per_doc),
                billed="n/a" if total_billed is None else _fmt_usd(total_billed),
            )
        )
        lines.append("")

    if card["provenance"]:
        lines.append("## Provenance")
        lines.append("")
        lines.append("| Key | Value |")
        lines.append("| --- | --- |")
        for key in sorted(card["provenance"]):
            lines.append(f"| {key} | {card['provenance'][key]} |")
        lines.append("")

    if card["figures"]:
        lines.append("## Figures")
        lines.append("")
        for caption, path in card["figures"].items():
            lines.append(f"![{caption}]({path})")
            lines.append("")

    if card["appendix_url"]:
        lines.append(f"Method and detail tables: [appendix]({card['appendix_url']}).")
        lines.append("")

    lines.append(
        "_`n/a` = not reported; no number is fabricated. **Busy-window GPU** is the "
        "cells' own GPU time; **metered** is the whole provider session (cold boots, "
        "gates, warm idle, teardown) from the billing report. Metric ids are never "
        "mixed in one row._"
    )
    return "\n".join(lines)
