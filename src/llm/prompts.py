"""Langfuse-managed agent prompts with graceful local fallback.

Every agent's system prompt lives in Langfuse Prompt Management (name
`mailroom-<agent_name>`, type `text`, `production` label). At runtime we fetch
the production version and compile its `{{variables}}` in; when Langfuse is not
configured or unreachable we fall back to the same template shipped in code, so
the pipeline behaves identically without observability — the local template is
always the floor.

Linking to traces: the fetched prompt object is returned alongside the compiled
text. Callers pass it to the OpenAI call via `langfuse_prompt=` (consumed by
the `langfuse.openai` instrumentation — never forwarded to the provider), so
every generation shows exactly which prompt version produced it.

Syncing: `scripts/sync_prompts.py` pushes the local templates up to Langfuse,
so code and the managed prompts never drift.
"""

import os
import time

import structlog

logger = structlog.get_logger(__name__)

PROMPT_PREFIX = "mailroom"

# (agent_name, label) -> (fetched_at, prompt object). Misses (None) are never
# stored, and entries expire after ``MAILROOM_PROMPT_CACHE_TTL`` seconds so a
# long-running watcher picks up a re-labelled `production` prompt.
_prompt_cache: dict[tuple[str, str], tuple[float, object]] = {}
_now = time.monotonic
# (agent_name, label) -> time of the last failed fetch. The SDK retries a miss
# (~2 s), so a never-synced prompt or a Langfuse outage must not pay that on
# every LLM call; the local fallback is served until this short TTL lapses.
_miss_cache: dict[tuple[str, str], float] = {}
MISS_TTL = 30.0


def prompt_cache_ttl() -> float:
    try:
        return float(os.environ.get("MAILROOM_PROMPT_CACHE_TTL", "60"))
    except ValueError:
        return 60.0


def prompt_name(agent_name: str) -> str:
    return f"{PROMPT_PREFIX}-{agent_name}"


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client
    from observability.tracing import resolve_provider_name

    if resolve_provider_name() != "langfuse":
        return None
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        return None
    return client


def render_template(text: str, variables: dict | None = None) -> str:
    """Substitute `{{var}}` placeholders in a local default template."""
    if not variables:
        return text
    out = text
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def get_managed_prompt(
    agent_name: str,
    default_text: str,
    variables: dict | None = None,
    label: str = "production",
) -> tuple[str, object | None]:
    """Return (compiled_prompt_text, prompt_obj_or_None).

    Prefers the Langfuse-managed prompt labeled `production`; falls back to
    `default_text` (rendered with `variables`) when unavailable.
    """
    cache_key = (agent_name, label)
    cached = _prompt_cache.get(cache_key)
    if cached is not None and _now() - cached[0] >= prompt_cache_ttl():
        cached = None
    prompt_obj = cached[1] if cached is not None else None
    missed_at = _miss_cache.get(cache_key)
    recently_missed = missed_at is not None and _now() - missed_at < MISS_TTL
    if prompt_obj is None and not recently_missed:
        client = _client()
        if client is not None:
            try:
                prompt_obj = client.get_prompt(prompt_name(agent_name), label=label)
            except Exception:
                logger.warning("prompt_fetch_failed", agent=agent_name, label=label, exc_info=True)
                prompt_obj = None
        if prompt_obj is not None:
            _prompt_cache[cache_key] = (_now(), prompt_obj)
            _miss_cache.pop(cache_key, None)
        else:
            _prompt_cache.pop(cache_key, None)
            if client is not None:
                _miss_cache[cache_key] = _now()

    if prompt_obj is not None:
        try:
            compiled = prompt_obj.compile(**variables) if variables else prompt_obj.prompt
            return compiled, prompt_obj
        except Exception:
            logger.warning("prompt_compile_failed", agent=agent_name, exc_info=True)
    return render_template(default_text, variables), None


def _langchain_prompt(version: str) -> str:
    """Local template for the vendored LangChain agents' versioned prompts
    (langchain_agents/prompts.py, committed with the vendored stack).

    Reads ``PROMPT_VERSIONS`` directly so the production catalog stays the
    agent-name-pinned production surface.
    """
    from langchain_agents.prompts import PROMPT_VERSIONS

    return PROMPT_VERSIONS[version]


def _bound_prompt_versions() -> dict[str, str]:
    """Version keys currently wired into production / agent defaults.

    Used for catalog metadata. Langfuse-managed BaseAgent prompts are the
    ``production`` label of ``mailroom-<agent_name>``; vendored LangChain
    agents pin an explicit lineage key.
    """
    return {
        "sorter": "sorter_v14",
        "sorter_reviewer": "production",
        # Specialists: sandbox / eval-environment frozen v1. Entity-extraction
        # contracts_specialist_v33 remains an eval pin only. Sorter stays v14.
        "contracts_specialist": "frozen_v1",
        "merger_agreement_specialist": "frozen_v1",
        "corporate_records_specialist": "frozen_v1",
        "correspondence_specialist": "frozen_v1",
        "insurance_claims_specialist": "frozen_v1",
        "boss": "production",
        "reporter": "production",
        "pdf_transcriber": "production",
        "image_extractor": "production",
        "judge": "production",
        "judge-classification": "production",
        "judge-correctness": "production",
        "arbiter": "production",
        "gmail_triage": "production",
        "intake": "production",
        "relations": "production",
    }


def prompt_templates() -> dict[str, str]:
    """agent_name -> local prompt template (with `{{var}}` placeholders).

    Single source of truth for `scripts/sync_prompts.py`. Imported lazily to
    avoid import cycles with the agent modules.
    """
    from agents import (  # noqa: F401
        arbiter,
        boss,
        contracts_specialist,
        corporate_records_specialist,
        correspondence_specialist,
        gmail_triage,
        insurance_claims_specialist,
        merger_agreement_specialist,
        image_extractor,
        intake,
        judge,
        pdf_transcriber,
        relations,
        reporter,
        sorter,
        sorter_reviewer,
    )

    from llm.frozen_v1 import load_specialist_v1

    return {
        # Sorter: best classify prompt this pipeline has (V12 CUAD-subtype
        # lineage + mailroom doctrine). Specialists: frozen v1 stems from
        # the sandbox / eval-environment lineage — LangGraph extract nodes
        # and Langfuse `mailroom-<agent>` production share these bytes.
        "sorter": _langchain_prompt("sorter_v14"),
        "sorter_reviewer": sorter_reviewer.REVIEWER_SYSTEM_PROMPT,
        "contracts_specialist": load_specialist_v1("contracts_specialist"),
        "merger_agreement_specialist": load_specialist_v1("merger_agreement_specialist"),
        "corporate_records_specialist": load_specialist_v1("corporate_records_specialist"),
        "correspondence_specialist": load_specialist_v1("correspondence_specialist"),
        "insurance_claims_specialist": load_specialist_v1("insurance_claims_specialist"),
        "boss": boss.BOSS_SYSTEM_PROMPT,
        "reporter": reporter.COMPILE_SYSTEM_PROMPT,
        "pdf_transcriber": pdf_transcriber.SYSTEM_PROMPT,
        "image_extractor": image_extractor.SYSTEM_PROMPT,
        "judge": judge.SYSTEM_PROMPT,
        "judge-classification": judge.CLASSIFICATION_SYSTEM_PROMPT,
        "judge-correctness": judge.CORRECTNESS_SYSTEM_PROMPT,
        "arbiter": arbiter.ARBITER_SYSTEM_PROMPT,
        "gmail_triage": gmail_triage.TRIAGE_SYSTEM_PROMPT,
        "intake": intake.INTAKE_SYSTEM_PROMPT,
        "relations": relations.RELATIONS_SYSTEM_PROMPT,
    }
