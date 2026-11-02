"""Unified score emitter — ONE routing layer for scores instead of each repo
calling observability backends directly.

Sits ON TOP of the calculation modules (nothing here computes metrics):

- :func:`register_metric` / the YAML registry define WHAT exists.
- :class:`Emitter` routes computed values to sinks: a network-free
  :class:`LocalManifestSink` (JSONL, always available) and an optional
  :class:`LangfuseSink` (no-ops when the SDK/config is absent, matching
  llm-mailroom's observability facade philosophy).

Typical consumer (a ``score_emitter`` bridge module, as in the former llm-entity-extraction)::

    from llm_dojo_scoring.emitter import Emitter, LocalManifestSink

    emitter = Emitter(sinks=[LocalManifestSink("reports/scores.jsonl")])
    emitter.emit_score(
        "sorter", doc_id="doc_17", metric_name="f1_macro", value=0.93,
        provenance={"dataset_revision": "46a4d3c2", "prompt_id": "sorter_v3",
                    "serving_kind": "api", "model_id": "qwen/qwen3-8b"},
    )
    card = emitter.scorecard("sorter", run_id="run_42", min_tier=0)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Protocol

from .registry import MetricTier, Registry, load_registry

_log = logging.getLogger("llm_dojo_scoring.emitter")

# Live-or-loud (DMR-061): a configured sink's failures are COUNTED and logged,
# never silently dropped — the "inert-unless-configured" layer must stay loud
# once configured.
SINK_EMIT_FAILURES = 0
SINK_FLUSH_FAILURES = 0


def sink_failure_counts() -> dict[str, int]:
    """Test/seam view: how many score emissions / flushes were lost to sinks."""
    return {
        "emit_failures": SINK_EMIT_FAILURES,
        "flush_failures": SINK_FLUSH_FAILURES,
    }


def _wire_score_name(name: str) -> str:
    """Langfuse transport name (35-char config limit aliases)."""
    from .mailroom import langfuse_score_name

    return langfuse_score_name(name)

__all__ = [
    "ScoreRecord",
    "ScoreSink",
    "LocalManifestSink",
    "LangfuseSink",
    "Emitter",
    "EMIT_REQUIRED_PROVENANCE",
    "get_emitter",
    "reset_default_emitter",
]


@dataclass(frozen=True)
class ScoreRecord:
    """One emitted score. Plain data — JSON-serializable via ``as_dict``."""

    agent: str
    metric: str
    value: float | str | None
    doc_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        """Return all record fields as a JSON-serializable mapping."""
        return {
            "agent": self.agent,
            "metric": self.metric,
            "value": self.value,
            "doc_id": self.doc_id,
            "run_id": self.run_id,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreRecord":
        """Construct a record from recognized fields, ignoring unknown keys."""
        known = {k: data[k] for k in ("agent", "metric", "value", "doc_id", "run_id", "metadata", "timestamp") if k in data}
        return cls(**known)


class ScoreSink(Protocol):
    """Anything that accepts records and can flush."""

    def emit(self, record: ScoreRecord) -> None:
        """Accept one score record for delivery or storage."""
        ...

    def flush(self) -> None:
        """Complete pending score deliveries before returning."""
        ...


class LocalManifestSink:
    """Append-only JSONL manifest. Network-free, always available."""

    def __init__(self, path: str | Path | None = None) -> None:
        """Resolve the manifest path and create its parent directory."""
        self.path = Path(
            path
            or os.environ.get("LLM_DOJO_SCORING_MANIFEST")
            or Path.home() / ".llm_dojo" / "scores.jsonl"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: ScoreRecord) -> None:
        """Append one score record as a sorted-key JSON line."""
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")

    def flush(self) -> None:  # pragma: no cover - nothing buffered
        """Do nothing because each record is written and closed immediately."""
        return None

    def read_all(self) -> list[ScoreRecord]:
        """Read records in manifest order, returning an empty list if absent."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(ScoreRecord.from_dict(json.loads(line)))
        return out


class LangfuseSink:
    """Optional Langfuse sink — silently inert when the SDK is unavailable.

    ``client`` may be any object exposing ``score(trace_id=..., name=...,
    value=..., data_type=...)``-style calls; when omitted, the constructor
    tries ``langfuse.Langfuse()`` lazily. On any failure the sink marks
    itself unavailable and ``emit`` becomes a no-op (never raises into the
    scoring path).
    """

    def __init__(self, client: Any | None = None, *, strict: bool = False) -> None:
        """Use an injected client or initialize the SDK, optionally raising on setup failure."""
        self._client = client
        self.available = True
        if self._client is None:
            # Credential precheck: a keyless SDK builds a silently disabled
            # client, so require explicit creds before constructing one.
            # Consumers with file-based key setups pass ``client=`` directly.
            import os

            if not (
                os.environ.get("LANGFUSE_PUBLIC_KEY")
                or os.environ.get("LANGFUSE_SECRET_KEY")
            ):
                self.available = False
                if strict:
                    raise RuntimeError("LangfuseSink: no LANGFUSE_PUBLIC_KEY/SECRET_KEY configured")
                return
            try:
                from langfuse import Langfuse  # type: ignore[import-not-found]

                self._client = Langfuse()
            except Exception:  # pragma: no cover - depends on env
                self.available = False
                if strict:
                    raise

    def emit(self, record: ScoreRecord) -> None:
        """Send a score when available, logging and counting delivery failures."""
        if not self.available or self._client is None:
            return
        try:
            self._client.score(
                trace_id=record.metadata.get("trace_id"),
                name=_wire_score_name(record.metric),
                value=record.value,
                data_type=record.metadata.get("data_type", "NUMERIC"),
                comment=record.metadata.get("comment"),
            )
        except Exception as exc:  # never fatal — but never silent either (DMR-061)
            global SINK_EMIT_FAILURES
            SINK_EMIT_FAILURES += 1
            _log.error(
                "LangfuseSink.emit FAILED for metric %r value=%r (trace_id=%r) — "
                "this score record is LOST to the sink; %d emit failure(s) so far",
                record.metric,
                record.value,
                record.metadata.get("trace_id"),
                SINK_EMIT_FAILURES,
                exc_info=exc,
            )

    def flush(self) -> None:
        """Flush an available client, logging and counting failures without raising."""
        if self.available and self._client is not None:
            try:
                self._client.flush()
            except Exception as exc:  # never fatal — but never silent either (DMR-061)
                global SINK_FLUSH_FAILURES
                SINK_FLUSH_FAILURES += 1
                _log.error(
                    "LangfuseSink.flush FAILED — buffered score records may be "
                    "lost; %d flush failure(s) so far",
                    SINK_FLUSH_FAILURES,
                    exc_info=exc,
                )


def _aggregate(values: list[Any], mode: str) -> Any:
    """Apply sum, last-value, or numeric-mean aggregation with nonnumeric fallbacks."""
    if not values:
        return None
    numeric = [v for v in values if isinstance(v, (int, float))]
    if mode == "sum":
        return float(sum(numeric)) if numeric else None
    if mode == "none":
        return values[-1]
    # default: mean over numeric values
    return float(fmean(numeric)) if numeric else values[-1]


#: Provenance keys a caller must supply on every emitted score (#22).
#: ``scorer_version`` and ``metric_id`` are filled here; ``split`` and
#: ``draw_seed`` are recorded when given ("if any" in the issue).
EMIT_REQUIRED_PROVENANCE: tuple[str, ...] = (
    "dataset_revision",
    "prompt_id",
    "serving_kind",
    "model_id",
)


def _emit_provenance(
    metric_name: str,
    units: str,
    provenance: Mapping[str, Any] | None,
    *,
    require: bool,
) -> dict[str, Any]:
    """Normalize the caller's stamp; refuse an incomplete one when required."""
    from .scorecard_honesty import (
        _METRIC_ID_BY_NAME,
        COST_BASIS_VALUES,
        ProvenanceExportError,
        stamp_provenance,
    )

    given = dict(provenance or {})
    if isinstance(given.get("provenance"), Mapping):  # a stamped scorecard
        given = dict(given["provenance"])
    # Unmapped registry metrics (latency, cost, ...) have no cross-leg
    # comparison id; their identity is the registry entry itself.
    canonical = _METRIC_ID_BY_NAME.get(metric_name) or f"registry:{metric_name}"
    claimed = given.get("metric_id")
    if claimed is not None and claimed != canonical:
        raise ProvenanceExportError(
            f"provenance metric_id {claimed!r} contradicts {metric_name!r} "
            f"(canonical {canonical!r})"
        )
    prov = stamp_provenance(given, metric_id=canonical)["provenance"]
    if not require:
        return prov
    missing = [k for k in EMIT_REQUIRED_PROVENANCE if not prov.get(k)]
    invalid = ""
    if units == "USD":
        basis = prov.get("cost_basis")
        if not basis:
            missing.append("cost_basis")
        elif basis not in COST_BASIS_VALUES:
            invalid = (
                f"cost_basis {basis!r} is not one of {sorted(COST_BASIS_VALUES)}"
            )
    if missing or invalid:
        parts = [f"provenance missing {missing}"] if missing else []
        if invalid:
            parts.append(invalid)
        raise ProvenanceExportError(
            f"emit_score({metric_name!r}) refused: " + "; ".join(parts)
        )
    return prov


class Emitter:
    """Routes score records to sinks and answers scorecard queries.

    Every record carries a provenance stamp in ``metadata["provenance"]``
    (#22). With ``require_provenance=True`` (the default) a score without
    :data:`EMIT_REQUIRED_PROVENANCE` — plus ``cost_basis`` on USD metrics —
    raises :class:`~llm_dojo_scoring.scorecard_honesty.ProvenanceExportError`
    before reaching any sink. Pass ``require_provenance=False`` only for
    ad-hoc local exploration whose records are never compared.
    """

    def __init__(
        self,
        sinks: Iterable[ScoreSink] | None = None,
        *,
        registry: Registry | None = None,
        require_provenance: bool = True,
    ) -> None:
        """Configure score sinks, metric registry, and required provenance validation."""
        self.sinks: list[ScoreSink] = list(sinks) if sinks is not None else [LocalManifestSink()]
        self.registry = registry or load_registry()
        self.require_provenance = require_provenance
        self._records: list[ScoreRecord] = []

    # -- emitting ------------------------------------------------------------

    def register_metric(self, name: str, tier: int | MetricTier, **spec: Any) -> None:
        """Register an ad-hoc metric definition (in-memory)."""
        from .registry import MetricDef

        self.registry.metrics[name] = MetricDef(
            name=name, tier=MetricTier(tier), **spec
        )

    def emit_score(
        self,
        agent_name: str,
        doc_id: str | None,
        metric_name: str,
        value: float | str | None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> ScoreRecord:
        """Validate against the registry and provenance, then fan out.

        ``provenance`` is a bare stamp or a scorecard already passed through
        :func:`~llm_dojo_scoring.scorecard_honesty.stamp_provenance`.
        """
        mdef = self.registry.get(metric_name)  # KeyError on unknown metric: fail fast
        meta = dict(metadata or {})
        if provenance is None and isinstance(meta.get("provenance"), Mapping):
            provenance = meta["provenance"]
        meta["provenance"] = _emit_provenance(
            metric_name,
            mdef.units,
            provenance,
            require=self.require_provenance,
        )
        record = ScoreRecord(
            agent=agent_name,
            metric=metric_name,
            value=value,
            doc_id=doc_id,
            run_id=run_id,
            metadata=meta,
        )
        self._records.append(record)
        for sink in self.sinks:
            sink.emit(record)
        return record

    def flush(self) -> None:
        """Flush each configured score sink in order."""
        for sink in self.sinks:
            sink.flush()

    # -- queries ---------------------------------------------------------------

    def _run_records(self, agent: str, run_id: str | None) -> list[ScoreRecord]:
        """Select buffered records for an agent and optionally a specific run."""
        return [
            r
            for r in self._records
            if r.agent == agent and (run_id is None or r.run_id == run_id)
        ]

    def get_scorecard(
        self,
        agent_name: str,
        run_id: str | None = None,
        min_tier: int | MetricTier = MetricTier.LOG,
    ) -> dict[str, Any]:
        """Aggregated per-metric scorecard for an agent (optionally one run).

        ``min_tier=1`` keeps headline+core only — the dashboard default.
        """
        max_tier = MetricTier(min_tier)
        defs = {
            m.name: m
            for m in self.registry.filter(max_tier=max_tier, agent=agent_name)
        }
        agg: dict[str, list[Any]] = {}
        for r in self._run_records(agent_name, run_id):
            if r.metric in defs:
                agg.setdefault(r.metric, []).append(r.value)
        return {
            name: _aggregate(vals, defs[name].aggregation)
            for name, vals in sorted(agg.items())
        }

    def compare_headlines(
        self,
        agent_a: str,
        agent_b: str,
        run_id_a: str | None = None,
        run_id_b: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Compare two agents/runs on their T0 headline metrics."""
        card_a = self.get_scorecard(agent_a, run_id_a, min_tier=MetricTier.HEADLINE)
        card_b = self.get_scorecard(agent_b, run_id_b, min_tier=MetricTier.HEADLINE)
        out: dict[str, dict[str, Any]] = {}
        for name in sorted(set(card_a) | set(card_b)):
            va, vb = card_a.get(name), card_b.get(name)
            delta = (
                round(vb - va, 6)
                if isinstance(va, (int, float)) and isinstance(vb, (int, float))
                else None
            )
            out[name] = {"a": va, "b": vb, "delta_b_minus_a": delta}
        return out


_DEFAULT_EMITTER: Emitter | None = None


def get_emitter() -> Emitter:
    """Process-wide default emitter (local manifest sink + registry)."""
    global _DEFAULT_EMITTER
    if _DEFAULT_EMITTER is None:
        _DEFAULT_EMITTER = Emitter()
    return _DEFAULT_EMITTER


def reset_default_emitter() -> None:
    """Discard the process-wide emitter so the next lookup creates a new one."""
    global _DEFAULT_EMITTER
    _DEFAULT_EMITTER = None
