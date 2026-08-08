#!/usr/bin/env python3
"""Configure the LLM-as-a-Judge evaluators in the connected Langfuse project.

The pipeline emits exactly one cumulative `pipeline-result` generation per
document trace (see `graph/build_graph.py:_emit_pipeline_result`), and this
script deploys the single evaluator + single observation rule that score it:

  - `mailroom-pipeline-judge` → one judge call per document returning a binary
    CORRECT/MISS verdict. When ground truth is available (pilot runs pass the
    manifest's `expected_doc_class`/`expected_stage` through the generation
    output), the judge decides STRICTLY against the ACTUAL truth; otherwise
    (live runs) it falls back to rubric judgment against the taxonomy spec and
    the document text.
  - `mailroom-pipeline-rule` → observation rule matching the `pipeline-result`
    generation name, so each document costs exactly ONE judge call.

No per-agent judges exist: scoring every specialist/sorter generation
separately tripled judge cost per document and multiplied rule maintenance.
The offline judges (`agents/judge.py`, `scripts/run_quality_judges.py`) still
provide per-dimension deep audits for pilot runs.

The rule maps the evaluator prompt's `{{input}}`/`{{output}}` variables to the
generation's input (document text) and output (curated pipeline result, which
also carries `ground_truth` when the caller knows the expected outcome).

The judge model defaults to the taxonomy's `judge` agent mapping; override with
--provider/--model. Requires the project's LLM connections (or an explicit
modelConfig) to run the judge — see the API reference for LLM Connections.

Usage:
    python scripts/sync_evaluators.py               # create/update evaluator + rule
    python scripts/sync_evaluators.py --dry-run     # preview
    python scripts/sync_evaluators.py --force       # always create new versions
    python scripts/sync_evaluators.py --disable     # disable rule instead of enabling
"""

from __future__ import annotations

import argparse
import os
import sys

import structlog

logger = structlog.get_logger(__name__)

import httpx


def _post_or_report(action: str, fn, *args, **kwargs):
    """Run a create/update call. The API sometimes never returns the response
    body (the preflight keeps the request open), so a read timeout is treated
    as 'submitted' — the final state is verified via list afterwards."""
    try:
        fn(*args, **kwargs)
        return True
    except httpx.ReadTimeout:
        logger.warning("langfuse_call_timed_out_assumed_submitted", action=action)
        return False
    except Exception:
        logger.warning("langfuse_call_failed", action=action, exc_info=True)
        return False

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

# The generation observation the single rule matches — one per document trace.
PIPELINE_RESULT_GENERATION = "pipeline-result"

# The evaluator name is used verbatim as the attached score name.
EVALUATOR_NAME = "mailroom-pipeline-judge"
RULE_NAME = "mailroom-pipeline-rule"

# Evaluator creation triggers a server-side preflight that calls the judge
# model (which can be slow) — give the API generous timeouts.
_REQUEST_OPTS = {"timeout": 900}

_TAXONOMY_SPEC = """\
- contract (Contract / Agreement): Formal agreements between parties: M&A, vendor, employment, NDAs, service agreements, leases, licensing
- corporate_record (Corporate Record): Bylaws, resolutions, board minutes, cap table entries, incorporation docs
- due_diligence (Due Diligence): Checklists, disclosure schedules, diligence memos, risk assessments
- correspondence (Correspondence): Letters, emails, memos, notices between parties or with regulators
- compliance_filing (Compliance Filing): SEC filings, state registrations, regulatory submissions, annual reports
- court_opinion (Court Opinion): Judicial opinions, orders, and decisions issued by courts"""

# One cumulative judge call per document returning a binary CORRECT/MISS
# verdict. With ground truth (pilot runs embed `expected_doc_class` /
# `expected_stage` in the generation output) the judge decides strictly
# against the ACTUAL truth; without it (live runs) it falls back to rubric
# judgment against the taxonomy + document text.
PIPELINE_PROMPT = f"""You are an expert legal reviewer auditing an automated legal-document mailroom pipeline against its ground truth.

Task specification — the pipeline must assign every incoming document exactly one of these classes:
{_TAXONOMY_SPEC}

The source document text is in {{{{input}}}}. The complete pipeline result is in {{{{output}}}}, containing:
- `doc_type` + `classification_confidence`: the assigned class and its confidence
- `extracted_data`: the structured fields extracted for that class (empty/absent for failed runs)
- `stage`, `escalation_reason`, `review_decision`, `error_message`: how the run ended
- `ground_truth` (when available): the EXPECTED outcome — `expected_doc_class` and `expected_stage` from the ground-truth manifest

Decide a single binary verdict:

1. If `ground_truth` is provided (pilot/evaluation runs), judge STRICTLY against the actual truth:
   - CORRECT only if ALL of these hold: the assigned `doc_type` equals `expected_doc_class`; the run reached the expected stage; and every field the document states (for that class's extraction schema) was captured completely and accurately — no fabrication, no paraphrase that changes meaning.
   - MISS otherwise: wrong class, failed/aborted run, fabricated or wrong extracted values, or a materially missing field the document states.

2. If no `ground_truth` is provided (live production runs), judge by rubric:
   - CORRECT: the assigned class is clearly the best fit for the document AND the extraction is complete and accurate (no fabrication, no materially missing stated fields).
   - MISS: a different available class clearly fits better, or the extraction contains fabrication/wrong values, or the run failed/aborted.

Return exactly one label — CORRECT or MISS. In the reasoning, cite the specific evidence: the classification verdict, any fabricated or wrong values, and any missing fields."""

EVALUATORS = [
    {
        "name": EVALUATOR_NAME,
        "prompt": PIPELINE_PROMPT,
        "output": ("CATEGORICAL", ["CORRECT", "MISS"]),
        "reasoning": "Evidence for the verdict: classification match, fabricated/wrong values, missing fields.",
        "score_description": "Binary run verdict (CORRECT/MISS): pipeline result matches the ground-truth class and contents (or, live, the task spec).",
    },
]


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing) — cannot configure evaluators.")
        return None
    return client


def _build_output_definition(output_spec, spec: dict):
    from langfuse.api.unstable.commons.types.evaluator_output_definition import (
        EvaluatorOutputDefinition_Categorical,
        EvaluatorOutputDefinition_Numeric,
    )
    from langfuse.api.unstable.commons.types.evaluator_output_field_definition import (
        EvaluatorOutputFieldDefinition,
    )
    from langfuse.api.unstable.commons.types.public_categorical_evaluator_output_score_definition import (
        PublicCategoricalEvaluatorOutputScoreDefinition,
    )

    data_type, categories = output_spec
    reasoning = EvaluatorOutputFieldDefinition(description=spec["reasoning"])
    if data_type == "CATEGORICAL":
        return EvaluatorOutputDefinition_Categorical(
            data_type=data_type,
            reasoning=reasoning,
            score=PublicCategoricalEvaluatorOutputScoreDefinition(
                description=spec["score_description"],
                categories=categories,
                should_allow_multiple_matches=False,
            ),
        )
    return EvaluatorOutputDefinition_Numeric(
        data_type=data_type,
        reasoning=reasoning,
        score=EvaluatorOutputFieldDefinition(description=spec["score_description"]),
    )


def _build_evaluator_request(spec: dict, provider: str, model: str):
    from langfuse.api.unstable.commons.types.evaluator_model_config import EvaluatorModelConfig
    from langfuse.api.unstable.evaluators.types.create_evaluator_request import (
        CreateEvaluatorRequest_LlmAsJudge,
    )

    return CreateEvaluatorRequest_LlmAsJudge(
        type="llm_as_judge",
        name=spec["name"],
        prompt=spec["prompt"],
        output_definition=_build_output_definition(spec["output"], spec),
        model_config_=EvaluatorModelConfig(provider=provider, model=model),
    )


def _ensure_llm_connection(client, *, provider: str, model: str) -> bool:
    """Make sure the judge provider has an LLM connection in the project.

    LLM-as-a-judge evaluators need project credentials for the judge model.
    We create an OpenAI-adapter connection for OpenRouter using
    OPENROUTER_API_KEY from the environment (never printed). Returns True when
    the connection exists after the call.
    """
    existing = {}
    try:
        page = client.api.llm_connections.list(limit=100, request_options=_REQUEST_OPTS)
        existing = {c.provider: c for c in (page.data or [])}
    except Exception:
        pass
    if provider in existing:
        return True

    if provider != "openrouter":
        print(f"No LLM connection for provider '{provider}' — configure it under "
              "Settings -> LLM Connections, or use --provider openrouter.")
        return False

    import os

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY is not set — cannot create the OpenRouter LLM connection.")
        return False
    client.api.llm_connections.upsert(
        provider="openrouter",
        adapter="openai",
        secret_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        custom_models=[model],
        with_default_models=True,
        request_options=_REQUEST_OPTS,
    )
    print("Created OpenRouter LLM connection (adapter=openai) for the judge provider.")
    return True


def _current_evaluator_prompt(client, name: str) -> str | None:
    """Return the prompt of the latest version of `name`, if any.

    The list response does not reliably populate `isLatest`, so we compare the
    highest-version entry by `version` directly.
    """
    try:
        page = client.api.unstable.evaluators.list(limit=100, request_options=_REQUEST_OPTS)
        candidates = [ev for ev in (page.data or []) if ev.name == name]
        if not candidates:
            return None
        latest = max(candidates, key=lambda ev: getattr(ev, "version", 0) or 0)
        return latest.prompt
    except Exception:
        return None


def sync_evaluators(client, *, provider: str, model: str, force: bool, dry_run: bool) -> int:
    changed = 0
    for spec in EVALUATORS:
        current = None if force else _current_evaluator_prompt(client, spec["name"])
        if current == spec["prompt"]:
            print(f"unchanged  {spec['name']}")
            continue
        if dry_run:
            print(f"would sync {spec['name']}")
            changed += 1
            continue
        ok = _post_or_report(
            f"create evaluator {spec['name']}",
            client.api.unstable.evaluators.create,
            request=_build_evaluator_request(spec, provider, model),
            request_options=_REQUEST_OPTS,
        )
        print(f"synced     {spec['name']}" + ("" if ok else " (submitted, verify below)"))
        changed += 1
    return changed


def _build_rule_request(evaluator_name: str, generation_name: str, enabled: bool = True):
    from langfuse.api.unstable.commons.types.evaluation_rule_filter import (
        EvaluationRuleFilter_StringOptions,
    )
    from langfuse.api.unstable.commons.types.evaluation_rule_mapping import (
        EvaluationRuleMapping,
    )
    from langfuse.api.unstable.commons.types.evaluation_rule_mapping_source import (
        EvaluationRuleMappingSource,
    )
    from langfuse.api.unstable.commons.types.evaluation_rule_options_filter_operator import (
        EvaluationRuleOptionsFilterOperator,
    )
    from langfuse.api.unstable.commons.types.evaluation_rule_target import (
        EvaluationRuleTarget,
    )
    from langfuse.api.unstable.commons.types.evaluator_scope import EvaluatorScope
    from langfuse.api.unstable.evaluation_rules.types.create_llm_as_judge_evaluation_rule_request import (
        CreateLlmAsJudgeEvaluationRuleRequest,
    )
    from langfuse.api.unstable.evaluation_rules.types.llm_as_judge_evaluation_rule_evaluator_reference import (
        LlmAsJudgeEvaluationRuleEvaluatorReference,
    )

    return CreateLlmAsJudgeEvaluationRuleRequest(
        name=RULE_NAME,
        evaluator=LlmAsJudgeEvaluationRuleEvaluatorReference(
            name=evaluator_name,
            scope=EvaluatorScope.PROJECT,
        ),
        target=EvaluationRuleTarget.OBSERVATION,
        enabled=enabled,
        sampling=1.0,
        filter=[
            EvaluationRuleFilter_StringOptions(
                type="stringOptions",
                column="name",
                operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                value=[generation_name],
            ),
            EvaluationRuleFilter_StringOptions(
                type="stringOptions",
                column="type",
                operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                value=["GENERATION"],
            ),
        ],
        mapping=[
            EvaluationRuleMapping(variable="input", source=EvaluationRuleMappingSource.INPUT),
            EvaluationRuleMapping(variable="output", source=EvaluationRuleMappingSource.OUTPUT),
        ],
    )


def _existing_rule_ids(client, rule_names: set[str]) -> dict[str, str]:
    found = {}
    try:
        page = client.api.unstable.evaluation_rules.list(limit=100, request_options=_REQUEST_OPTS)
        for rule in (page.data or []):
            if rule.name in rule_names:
                found[rule.name] = rule.id
    except Exception:
        pass
    return found


def sync_rules(client, *, enabled: bool, force: bool, dry_run: bool) -> int:
    """Ensure the single cumulative rule exists; prune all other mailroom rules."""
    existing = _existing_rule_ids(client, {RULE_NAME})
    changed = 0
    if RULE_NAME in existing and not force:
        print(f"rule exists {RULE_NAME}")
    elif dry_run:
        print(f"would sync {RULE_NAME}")
        changed += 1
    else:
        request = _build_rule_request(EVALUATOR_NAME, PIPELINE_RESULT_GENERATION, enabled=enabled)
        if RULE_NAME in existing:
            ok = _post_or_report(
                f"update rule {RULE_NAME}",
                client.api.unstable.evaluation_rules.update,
                existing[RULE_NAME],
                name=request.name,
                evaluator=request.evaluator,
                target=request.target,
                enabled=enabled,
                sampling=request.sampling,
                filter=request.filter,
                mapping=request.mapping,
                request_options=_REQUEST_OPTS,
            )
            print(f"updated    {RULE_NAME}" + ("" if ok else " (submitted, verify below)"))
        else:
            ok = _post_or_report(
                f"create rule {RULE_NAME}",
                client.api.unstable.evaluation_rules.create,
                request=request,
                request_options=_REQUEST_OPTS,
            )
            print(f"created    {RULE_NAME}" + ("" if ok else " (submitted, verify below)"))
        changed += 1

    if not dry_run:
        _prune_stale_rules(client, {RULE_NAME})
        _prune_stale_evaluators(client, {s["name"] for s in EVALUATORS})
    return changed


def _prune_stale_rules(client, wanted_names: set[str]) -> None:
    """Delete mailroom rules that are no longer in the spec (e.g. old
    per-agent classification/completeness/correctness rules)."""
    try:
        page = client.api.unstable.evaluation_rules.list(limit=100, request_options=_REQUEST_OPTS)
        stale = [r for r in (page.data or []) if r.name.startswith("mailroom-") and r.name not in wanted_names]
        for rule in stale:
            _post_or_report(
                f"delete rule {rule.name}",
                client.api.unstable.evaluation_rules.delete,
                rule.id,
                request_options=_REQUEST_OPTS,
            )
            print(f"pruned     {rule.name}")
    except Exception:
        logger.warning("rule_prune_failed", exc_info=True)


def _prune_stale_evaluators(client, wanted_names: set[str]) -> None:
    """Delete project-scope mailroom evaluators that are no longer deployed
    (the old per-dimension judges). Managed/template evaluators are left alone
    (platform-locked, API returns 403)."""
    try:
        page = client.api.unstable.evaluators.list(limit=100, request_options=_REQUEST_OPTS)
        stale = [
            ev for ev in (page.data or [])
            if ev.name.startswith("mailroom-") and ev.name not in wanted_names
        ]
        for ev in stale:
            _post_or_report(
                f"delete evaluator {ev.name}",
                client.api.unstable.evaluators.delete,
                ev.id,
                request_options=_REQUEST_OPTS,
            )
            print(f"pruned     evaluator {ev.name}")
    except Exception:
        logger.warning("evaluator_prune_failed", exc_info=True)


def verify(client) -> None:
    """Print the final state of evaluators and rules from the server."""
    print("\n== Final state (from Langfuse) ==")
    try:
        page = client.api.unstable.evaluators.list(limit=100, request_options=_REQUEST_OPTS)
        for name in [s["name"] for s in EVALUATORS]:
            versions = [ev for ev in (page.data or []) if ev.name == name]
            if versions:
                latest = max(versions, key=lambda ev: getattr(ev, "version", 0) or 0)
                print(f"evaluator  {name} v{latest.version}")
            else:
                print(f"evaluator  {name} NOT FOUND")
    except Exception:
        logger.warning("evaluator_verify_failed", exc_info=True)
    try:
        rules = client.api.unstable.evaluation_rules.list(limit=100, request_options=_REQUEST_OPTS)
        names = sorted({r.name for r in (rules.data or []) if r.name.startswith("mailroom-")})
        print(f"rules: {len(names)} mailroom rules")
        for n in names:
            print(f"  - {n}")
    except Exception:
        logger.warning("rule_verify_failed", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Langfuse LLM-as-a-Judge evaluators.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    parser.add_argument("--force", action="store_true", help="Create new evaluator versions / update rules even if unchanged.")
    parser.add_argument("--disable", action="store_true", help="Set rules to disabled instead of enabled.")
    parser.add_argument("--provider", default="openrouter", help="Judge provider (default: openrouter).")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash", help="Judge model (default: taxonomy judge model).")
    args = parser.parse_args()

    client = _client()
    if client is None:
        return 1

    if not args.dry_run:
        from pipeline.config import load_config

        judge_cfg = load_config().get("agents", {}).get("judge", {})
        provider = args.provider or judge_cfg.get("provider", "openrouter")
        model = args.model or judge_cfg.get("model", "deepseek/deepseek-v4-flash")
    else:
        provider, model = args.provider, args.model

    if not args.dry_run and not _ensure_llm_connection(client, provider=provider, model=model):
        return 1

    print("== Evaluators ==")
    sync_evaluators(client, provider=provider, model=model, force=args.force, dry_run=args.dry_run)
    print("\n== Evaluation rules (observations) ==")
    sync_rules(client, enabled=not args.disable, force=args.force, dry_run=args.dry_run)

    if not args.dry_run:
        from langfuse import get_client

        get_client().flush()
        verify(client)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
