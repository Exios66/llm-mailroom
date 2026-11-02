"""Agent profile system — one declarative registration per agent instead of
4+ file edits across repos.

A profile declares which task types an agent performs, which metric bundle it
uses (auto-selected from the bundle registry by task type when omitted), and
its aggregation rules. Profiles can be defined in code or loaded from a YAML
file (``LLM_DOJO_SCORING_PROFILES`` env var or explicit path).

Example YAML::

    agents:
      audit_agent:
        title: "Extraction Auditor"
        tasks: [verify_extraction]
        metrics_bundle: audit
        fallback_bundle: extraction

the default profile table covers every pipeline agent: sorter, eight
specialists (five live + three retired), reporter, judge, boss,
pdf_transcriber, image_extractor, archivist, intake clerk (pre-sorter
text prep), audit_agent, the review/audit lanes (sorter_reviewer,
per-specialist auditors, arbiter), the insurance_claims_auditor
companion, and the local_vs_api serving comparison.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .bundles import Bundle, get_bundle
from .registry import Registry

__all__ = [
    "AgentProfile",
    "DEFAULT_PROFILES",
    "get_profile",
    "list_profiles",
    "load_profiles",
    "clear_profile_cache",
]

_ENV_VAR = "LLM_DOJO_SCORING_PROFILES"


@dataclass(frozen=True)
class AgentProfile:
    """One agent's scoring identity."""

    name: str
    title: str = ""
    #: Task types performed: classify / extract / review / route / summarize /
    #: transcribe / verify / store / orchestrate / prepare / normalize.
    tasks: tuple[str, ...] = ()
    #: Primary metric bundle name (resolved via :func:`bundles.get_bundle`).
    metrics_bundle: str | None = None
    #: Used when the primary bundle cannot be applied (e.g. verification
    #: unavailable and the run degrades to plain classification).
    fallback_bundle: str | None = None
    #: KANBAN-067: optional doc-type bundle name (a ``DOC_TYPE_BUNDLES`` key,
    #: e.g. ``"insurance_claim"``) for doc-type-aware scoring. When None,
    #: :meth:`resolve_doc_bundle` degrades to the task bundle and SAYS SO via
    #: the returned ``used_fallback`` flag — never a silent default.
    doc_bundle: str | None = None
    #: How per-document scores roll up to run level.
    aggregation: str = "mean"
    #: Ground truth available for this agent's evaluation context?
    ground_truth: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    def resolve_bundle(
        self,
        *,
        fallback: bool = False,
        registry: Registry | None = None,
    ) -> Bundle:
        """Resolve the (validated) primary or fallback bundle."""
        name = self.fallback_bundle if fallback else (self.metrics_bundle or _bundle_for_tasks(self.tasks))
        if not name:
            raise ValueError(f"profile {self.name!r}: no metrics_bundle and no task-derived bundle")
        return get_bundle(name, registry=registry)

    def resolve_doc_bundle(
        self,
        doc_type: str | None = None,
        *,
        fallback: bool = True,
        registry: Registry | None = None,
    ) -> tuple[Bundle, bool]:
        """Resolve the doc-type bundle for this agent (KANBAN-067).

        Returns ``(bundle, used_fallback)``. Resolution order:

        1. ``doc_type`` given → ``doc:doc_type`` bundle from
           :data:`llm_dojo_scoring.doc_bundles.DOC_TYPE_BUNDLES`;
        2. else the profile's own ``doc_bundle`` field when set;
        3. else (``fallback=True``, the default) the task bundle with
           ``used_fallback=True`` — an EXPLICIT honesty marker for callers
           and dashboards, never a silent default;
        4. ``fallback=False`` with nothing to resolve raises ``ValueError``
           instead of pretending.
        """
        from .doc_bundles import get_doc_bundle

        if doc_type:
            return get_doc_bundle(doc_type, registry=registry), False
        if self.doc_bundle:
            return get_doc_bundle(self.doc_bundle, registry=registry), False
        if fallback:
            return self.resolve_bundle(registry=registry), True
        raise ValueError(
            f"profile {self.name!r}: no doc_bundle and no doc_type given "
            "(fallback disabled)"
        )


def _bundle_for_tasks(tasks: Iterable[str]) -> str | None:
    """Auto-select a bundle from task types (first match wins)."""
    table = {
        "classify": "classification",
        "route": "classification",
        "extract": "extraction",
        "review": "audit",
        "verify": "audit",
        "transcribe": "transcription",
        "prepare": "intake",
        "normalize": "intake",
        "store": "cost",
        "orchestrate": "reporter",
        "summarize": "reporter",
        "compare": "serving",
    }
    for t in tasks:
        if t in table:
            return table[t]
    return None


def _p(
    name: str,
    title: str,
    tasks: tuple[str, ...],
    bundle: str | None = None,
    **kw: Any,
) -> AgentProfile:
    """Construct a profile with its optional metrics bundle and extra settings."""
    return AgentProfile(name=name, title=title, tasks=tasks, metrics_bundle=bundle, **kw)


DEFAULT_PROFILES: dict[str, AgentProfile] = {
    p.name: p
    for p in (
        _p("sorter", "Document Router", ("classify", "route"), "classification"),
        _p(
            "contracts_specialist",
            "Contract Extraction",
            ("extract",),
            "extraction",
            doc_bundle="contract",
        ),
        _p(
            "merger_agreement_specialist",
            "Merger Agreement Extraction",
            ("extract",),
            "extraction",
            doc_bundle="merger_agreement",
        ),
        _p(
            "corporate_records_specialist",
            "Corporate Records Extraction",
            ("extract",),
            "extraction",
            doc_bundle="corporate_record",
        ),
        _p(
            "due_diligence_specialist",
            "Due-Diligence Extraction",
            ("extract",),
            "extraction",
            doc_bundle="due_diligence",
        ),
        _p(
            "correspondence_specialist",
            "Correspondence Parsing",
            ("extract",),
            "extraction",
            doc_bundle="correspondence",
        ),
        _p(
            "compliance_specialist",
            "Compliance Filing Extraction",
            ("extract",),
            "extraction",
            doc_bundle="compliance_filing",
        ),
        _p(
            "court_opinions_specialist",
            "Court Opinion Analysis",
            ("extract",),
            "extraction",
            doc_bundle="court_opinion",
        ),
        # KANBAN-067: the 23rd mailroom agent (Phase 1 added the class).
        _p(
            "insurance_claims_specialist",
            "Insurance Claim Extraction",
            ("extract",),
            "extraction",
            doc_bundle="insurance_claim",
        ),
        _p("reporter", "Run Reporting", ("summarize",), "reporter"),
        _p("judge", "Discretionary Adjudication", ("classify", "review"), "classification"),
        _p("boss", "Orchestration", ("orchestrate",), "reporter"),
        _p("pdf_transcriber", "PDF→Text", ("transcribe",), "transcription"),
        _p("image_extractor", "Image→Text/OCR", ("transcribe",), "transcription"),
        _p("archivist", "Storage/Indexing", ("store",), "cost", ground_truth=False),
        _p(
            "intake",
            "Intake Clerk",
            ("prepare", "normalize"),
            "intake",
            extras={
                "span": "normalize-intake",
                "observation_type": "span",
                "handoff_node": "classify-document",
                "handoff_agent": "sorter",
                "live_method": "deterministic",
                "methods": ["deterministic", "llm"],
                "prep_steps": [
                    "nfc_normalize",
                    "newline_unify",
                    "nbsp_to_space",
                    "strip_zero_width",
                    "control_chars",
                    "hyphen_unwrap",
                    "collapse_blank_runs",
                    "collapse_horizontal_space",
                    "trim_edges",
                ],
            },
        ),
        _p(
            "audit_agent",
            "Specialist Output Verification",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        # KANBAN-062/063 (architecture alignment): named review/audit lanes.
        # sorter_reviewer = agent second opinion after the sorter (Lane A);
        # per-specialist auditors + arbiter = the audit-manager pattern's
        # dispatch targets (Lane B escalation). Audit profiles never require
        # ground truth (they verify specialist output, not GT fields).
        _p(
            "sorter_reviewer",
            "Classification Review",
            ("classify", "review"),
            "classification",
        ),
        _p(
            "contract_auditor",
            "Contract Extraction Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "corporate_records_auditor",
            "Corporate Records Extraction Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "due_diligence_auditor",
            "Due-Diligence Extraction Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "correspondence_auditor",
            "Correspondence Parsing Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "compliance_auditor",
            "Compliance Filing Extraction Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "court_opinions_auditor",
            "Court Opinion Analysis Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "insurance_claims_auditor",
            "Insurance Claim Extraction Audit",
            ("verify", "review"),
            "audit",
            fallback_bundle="extraction",
            ground_truth=False,
        ),
        _p(
            "arbiter",
            "Judgment Arbitration",
            ("verify", "review"),
            "audit",
            ground_truth=False,
        ),
        _p(
            "local_vs_api",
            "Local vs API Serving Comparison",
            ("compare",),
            "serving",
            ground_truth=False,
            extras={
                "serving_kinds": ["local", "api"],
                "identity_fields": [
                    "model",
                    "quantization",
                    "dtype",
                    "max_model_len",
                    "gpu",
                    "provider",
                    "profile",
                ],
            },
        ),
    )
}


_CACHE: dict[str, dict[str, AgentProfile]] = {}


def load_profiles(path: str | Path | None = None) -> dict[str, AgentProfile]:
    """Load profiles: defaults overlaid with a YAML file when one resolves.

    Resolution order: explicit ``path`` > ``LLM_DOJO_SCORING_PROFILES`` env >
    built-in defaults only. YAML entries override same-name defaults; unknown
    bundle names fail fast through :func:`bundles.get_bundle`.
    """
    resolved = path or os.environ.get(_ENV_VAR)
    if not resolved:
        return dict(DEFAULT_PROFILES)
    key = str(Path(resolved).resolve())
    if key in _CACHE:
        return _CACHE[key]
    data = yaml.safe_load(Path(key).read_text(encoding="utf-8")) or {}
    profiles = dict(DEFAULT_PROFILES)
    for name, spec in (data.get("agents") or {}).items():
        spec = dict(spec or {})
        base = profiles.get(name)
        bundle = spec.get("metrics_bundle") or (base.metrics_bundle if base else None)
        tasks = tuple(spec.get("tasks") or (base.tasks if base else ()))
        # Validate bundle names eagerly.
        if bundle:
            get_bundle(bundle)
        fb = spec.get("fallback_bundle")
        if fb:
            get_bundle(fb)
        doc_bundle = spec.get("doc_bundle", base.doc_bundle if base else None)
        if doc_bundle:
            from .doc_bundles import get_doc_bundle

            get_doc_bundle(doc_bundle)
        profiles[name] = AgentProfile(
            name=name,
            title=str(spec.get("title", base.title if base else "")),
            tasks=tuple(tasks) if isinstance(tasks, (list, tuple)) else (str(tasks),),
            metrics_bundle=bundle,
            fallback_bundle=fb,
            doc_bundle=doc_bundle,
            aggregation=str(spec.get("aggregation", base.aggregation if base else "mean")),
            ground_truth=bool(spec.get("ground_truth", base.ground_truth if base else True)),
            extras=spec.get("extras", {}) or {},
        )
    _CACHE[key] = profiles
    return profiles


def get_profile(name: str, *, path: str | Path | None = None) -> AgentProfile:
    """Look up a loaded profile, listing known names in an unknown-name error."""
    try:
        return load_profiles(path)[name]
    except KeyError:
        raise KeyError(
            f"unknown agent profile {name!r}; known: {sorted(load_profiles(path))}"
        ) from None


def list_profiles(*, path: str | Path | None = None) -> list[str]:
    """Return sorted profile names from the selected profile catalog."""
    return sorted(load_profiles(path))


def clear_profile_cache() -> None:
    """Discard all cached profile catalogs so subsequent loads read them again."""
    _CACHE.clear()
