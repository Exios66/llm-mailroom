#!/usr/bin/env python3
"""Configure the LLM-as-a-Judge evaluators in the connected Langfuse project.

Creates (or updates, when the prompt changed) the evaluator family for each of
the three task-spec judges and the live-observation evaluation rules that wire
them to the pipeline's LLM generations:

  - `mailroom-classification-judge`       → scores the `sorter` generation
  - `mailroom-extraction-completeness-judge`  → scores specialist generations
  - `mailroom-extraction-correctness-judge`   → scores specialist generations

The rules target observation-level generations (the LLM calls, named after
their agent), mapping the evaluator prompt's `{{input}}`/`{{output}}` variables
to the generation's input/output, and attach the resulting score to that
generation in the trace. Score names follow the configs in
`observability/scores.py`.

The judge model defaults to the taxonomy's `judge` agent mapping; override with
--provider/--model. Requires the project's LLM connections (or an explicit
modelConfig) to run the judge — see the API reference for LLM Connections.

Usage:
    python scripts/sync_evaluators.py               # create/update evaluators + rules
    python scripts/sync_evaluators.py --dry-run     # preview
    python scripts/sync_evaluators.py --force       # always create new versions
    python scripts/sync_evaluators.py --disable     # disable rules instead of enabling
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

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

SPECIALIST_NAMES = [
    "contracts_specialist",
    "corporate_records_specialist",
    "due_diligence_specialist",
    "correspondence_specialist",
    "compliance_specialist",
]

# Evaluator creation triggers a server-side preflight that calls the judge
# model (which can be slow) — give the API generous timeouts.
_REQUEST_OPTS = {"timeout": 900}

_TAXONOMY_SPEC = """\
- contract (Contract / Agreement): Formal agreements between parties: M&A, vendor, employment, NDAs, service agreements, leases, licensing
- corporate_record (Corporate Record): Bylaws, resolutions, board minutes, cap table entries, incorporation docs
- due_diligence (Due Diligence): Checklists, disclosure schedules, diligence memos, risk assessments
- correspondence (Correspondence): Letters, emails, memos, notices between parties or with regulators
- compliance_filing (Compliance Filing): SEC filings, state registrations, regulatory submissions, annual reports"""

CLASSIFICATION_PROMPT = f"""You are an expert legal reviewer auditing an automated document-classification pipeline.

Task specification — the pipeline must assign every incoming document exactly one of these classes:
{_TAXONOMY_SPEC}

The document is provided in {{{{input}}}}. The pipeline's classification output is in {{{{output}}}}.

Judge whether the assigned class is correct for the document:
1. CORRECT if the assigned class clearly fits best, even if another class also plausibly fits.
2. INCORRECT if a different available class fits the document better.
3. AMBIGUOUS only for documents that genuinely span multiple classes with no clear best fit.

Return the classification label and short evidence-based reasoning."""

COMPLETENESS_PROMPT = """You are an expert legal reviewer evaluating extraction completeness.
The source document text is in {{input}}; the extraction output is in {{output}}.

Compare what was extracted against what the document actually states:
1. A field is COMPLETE if the document states the information and the extraction captured it.
2. A field is MISSING if the document states the information but the extraction left it empty.
3. A field is FABRICATED if the extraction reports information the document does not contain.
4. Judge only fields the extraction schema asks for; empty values for genuinely absent info are fine.

Score completeness as the fraction of expected fields that were correctly captured (0-1).
Return the score and a list of the specific gaps or fabrications."""

CORRECTNESS_PROMPT = """You are an expert legal reviewer auditing the factual accuracy of an automated
document-extraction run. The source document text is in {{input}}; the extraction output is in {{output}}.

Verify that every extracted field value is grounded in the document text — no fabrication, no
paraphrase that changes meaning, no values pulled from thin air.
1. ACCURATE means every populated field is supported by the document text and correct.
2. Empty fields are not errors by themselves — absence of wrong data is neutral.

Score factual accuracy as a 0-1 float (1.0 = fully accurate). Return the score and name the
specific fabricated or wrong values you found."""

EVALUATORS = [
    {
        "name": "mailroom-classification-judge",
        "prompt": CLASSIFICATION_PROMPT,
        "output": ("CATEGORICAL", ["correct", "incorrect", "ambiguous"]),
        "reasoning": "Short evidence-based explanation of the classification verdict.",
        "score_description": "Whether the assigned document class matches the task specification.",
    },
    {
        "name": "mailroom-extraction-completeness-judge",
        "prompt": COMPLETENESS_PROMPT,
        "output": ("NUMERIC", None),
        "reasoning": "Specific gaps or fabrications found in the extraction.",
        "score_description": "Fraction of expected extraction fields correctly captured.",
    },
    {
        "name": "mailroom-extraction-correctness-judge",
        "prompt": CORRECTNESS_PROMPT,
        "output": ("NUMERIC", None),
        "reasoning": "Specific fabricated or wrong values found in the extraction.",
        "score_description": "Factual accuracy of the extracted values (1.0 = fully accurate).",
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


def _build_rule_request(rule_name: str, evaluator_name: str, generation_name: str, enabled: bool = True):
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

    names = [generation_name] if generation_name == "sorter" else SPECIALIST_NAMES
    return CreateLlmAsJudgeEvaluationRuleRequest(
        name=rule_name,
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
                value=names,
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
    rule_specs = []
    # Classification judge → the sorter's generation.
    rule_specs.append(("mailroom-classification-rule", "mailroom-classification-judge", "sorter"))
    # Completeness/correctness judges → specialist extraction generations only.
    for gen in SPECIALIST_NAMES:
        for judge, suffix in (
            ("mailroom-extraction-completeness-judge", "completeness"),
            ("mailroom-extraction-correctness-judge", "correctness"),
        ):
            rule_specs.append((f"mailroom-{suffix}-rule-{gen}", judge, gen))

    existing = _existing_rule_ids(client, {name for name, _, _ in rule_specs})
    changed = 0
    for rule_name, evaluator_name, gen in rule_specs:
        if rule_name in existing and not force:
            print(f"rule exists {rule_name}")
            continue
        if dry_run:
            print(f"would sync {rule_name}")
            changed += 1
            continue
        request = _build_rule_request(rule_name, evaluator_name, gen, enabled=enabled)
        if rule_name in existing:
            ok = _post_or_report(
                f"update rule {rule_name}",
                client.api.unstable.evaluation_rules.update,
                existing[rule_name],
                name=request.name,
                evaluator=request.evaluator,
                target=request.target,
                enabled=enabled,
                sampling=request.sampling,
                filter=request.filter,
                mapping=request.mapping,
                request_options=_REQUEST_OPTS,
            )
            print(f"updated    {rule_name}" + ("" if ok else " (submitted, verify below)"))
        else:
            ok = _post_or_report(
                f"create rule {rule_name}",
                client.api.unstable.evaluation_rules.create,
                request=request,
                request_options=_REQUEST_OPTS,
            )
            print(f"created    {rule_name}" + ("" if ok else " (submitted, verify below)"))
        changed += 1

    if not dry_run:
        _prune_stale_rules(client, {name for name, _, _ in rule_specs})
    return changed


def _prune_stale_rules(client, wanted_names: set[str]) -> None:
    """Delete mailroom rules that are no longer in the spec (e.g. retargeted)."""
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
