"""Provider-independent Mailroom semantic contract. No SDK, config, or graph imports."""
from __future__ import annotations
from .scores import SCORE_CONFIGS, DERIVED_SCORE_CONFIGS, ALL_SCORE_CONFIGS, SCORE_BY_NAME, validate_score
from types import MappingProxyType

CONTRACT_VERSION = "2.0.0"
TRACE_NAME = "document-pipeline"
OBSERVATION_TYPES = frozenset({"chain", "span", "agent", "evaluator", "retriever", "generation"})
NODE_BINDINGS = MappingProxyType({
    'arbiter': ('arbitrate-verdict', 'agent'),
    'archive': ('archive-document', 'span'),
    'audit_failed': ('audit-failed', 'span'),
    'boss_escalation': ('adjudicate-conflict', 'agent'),
    'catalog_write': ('write-catalog', 'span'),
    'classification_arbiter': ('arbitrate-classification', 'agent'),
    'classify': ('classify-document', 'agent'),
    'compile_report': ('compile-report', 'agent'),
    'extract': ('extract-fields', 'agent'),
    'human_review': ('route-for-review', 'span'),
    'intake': ('intake-document', 'span'),
    'judge_verify': ('judge-verify', 'evaluator'),
    'retry_classify': ('classify-document', 'agent'),
    'retry_extract': ('extract-fields', 'agent'),
    'review_classify': ('classify-document', 'agent'),
    'score_common': ('score-common', 'evaluator'),
    'score_contract': ('score-contract', 'evaluator'),
    'score_corporate_record': ('score-corporate-record', 'evaluator'),
    'score_correspondence': ('score-correspondence', 'evaluator'),
    'score_generic': ('score-generic', 'evaluator'),
    'score_insurance_claim': ('score-insurance-claim', 'evaluator'),
    'score_merger_agreement': ('score-merger-agreement', 'evaluator'),
})
# Additional observations outside the 22-node graph. Node spans derive from
# NODE_BINDINGS so there is only one graph binding declaration.
_EXTRA_OBSERVATIONS = {
    "document-pipeline": "chain",
    "normalize-intake": "span",
    "intake-ml-triage": "span",
    "intake-llm-prep": "span",
    "extract-image-text": "retriever",
    "transcribe-pdf": "retriever",
    "pipeline-result": "generation",
    "answer-question": "generation",
}
OBSERVATION_BINDINGS = MappingProxyType({
    **{span: kind for span, kind in NODE_BINDINGS.values()},
    **_EXTRA_OBSERVATIONS,
})

def validate_observation(name: str, as_type: str | None = None) -> str:
    """Canonical names have fixed types; extension names may use any declared semantic type."""
    expected = OBSERVATION_BINDINGS.get(name)
    kind = as_type or expected or "span"
    if kind not in OBSERVATION_TYPES:
        raise ValueError(f"unknown Mailroom observation type: {kind!r}")
    if expected is not None and kind != expected:
        raise ValueError(f"observation {name!r} must be {expected!r}, got {kind!r}")
    return kind

def validate_node_binding(node_id: str, span: str, kind: str) -> None:
    expected = NODE_BINDINGS.get(node_id)
    if expected is None:
        raise ValueError(f"unknown Mailroom graph node: {node_id!r}")
    validate_observation(span, kind)
    if (span, kind) != expected:
        raise ValueError(f"graph node {node_id!r} must bind {expected!r}, got {(span, kind)!r}")
