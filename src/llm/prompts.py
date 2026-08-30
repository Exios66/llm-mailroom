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

import structlog

logger = structlog.get_logger(__name__)

PROMPT_PREFIX = "mailroom"

# (agent_name, label) -> fetched prompt object (None when unavailable)
_prompt_cache: dict[tuple[str, str], object | None] = {}


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

    When ``MAILROOM_DOCCLASS_PROMPTS`` is on, fetch the namespaced
    ``mailroom-docclass-<key>`` variant and fall back to the in-repo append.
    """
    try:
        from pipeline.docclass_mode import managed_prompt_lookup

        agent_name, default_text = managed_prompt_lookup(agent_name, default_text)
    except Exception:
        pass
    cache_key = (agent_name, label)
    if cache_key not in _prompt_cache:
        client = _client()
        prompt_obj = None
        if client is not None:
            try:
                prompt_obj = client.get_prompt(prompt_name(agent_name), label=label)
            except Exception:
                logger.warning("prompt_fetch_failed", agent=agent_name, label=label, exc_info=True)
                prompt_obj = None
        _prompt_cache[cache_key] = prompt_obj

    prompt_obj = _prompt_cache[cache_key]
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

    Reads ``PROMPT_VERSIONS`` directly so the production catalog never
    rewrites through the docclass arm (``prompt_templates()`` must stay
    the agent-name-pinned production surface).
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
        "contracts_specialist": "contracts_specialist_v32",
        "corporate_records_specialist": "production",
        "correspondence_specialist": "production",
        "compliance_specialist": "production",
        "insurance_claims_specialist": "production",
        "boss": "production",
        "reporter": "production",
        "pdf_transcriber": "production",
        "image_extractor": "production",
        "judge": "production",
        "judge-classification": "production",
        "judge-correctness": "production",
        "arbiter": "production",
    }


def prompt_templates() -> dict[str, str]:
    """agent_name -> local prompt template (with `{{var}}` placeholders).

    Single source of truth for `scripts/sync_prompts.py`. Imported lazily to
    avoid import cycles with the agent modules.
    """
    from agents import (  # noqa: F401
        arbiter,
        boss,
        compliance_specialist,
        contracts_specialist,
        corporate_records_specialist,
        correspondence_specialist,
        insurance_claims_specialist,
        image_extractor,
        judge,
        pdf_transcriber,
        reporter,
        sorter,
        sorter_reviewer,
    )

    return {
        # The sorter/contracts specialist are the vendored LangChain agents
        # (llm-entity-extraction); their local templates are the eval-validated
        # lineage plus the mailroom production mutation (sorter_v14 /
        # contracts_specialist_v32). Lane A/B + insurance were previously
        # missing from this registry and so never synced to Langfuse.
        "sorter": _langchain_prompt("sorter_v14"),
        "sorter_reviewer": sorter_reviewer.REVIEWER_SYSTEM_PROMPT,
        "contracts_specialist": _langchain_prompt("contracts_specialist_v33"),
        "corporate_records_specialist": corporate_records_specialist.SYSTEM_PROMPT,
        "correspondence_specialist": correspondence_specialist.SYSTEM_PROMPT,
        "compliance_specialist": compliance_specialist.SYSTEM_PROMPT,
        "insurance_claims_specialist": insurance_claims_specialist.SYSTEM_PROMPT,
        "boss": boss.BOSS_SYSTEM_PROMPT,
        "reporter": reporter.COMPILE_SYSTEM_PROMPT,
        "pdf_transcriber": pdf_transcriber.SYSTEM_PROMPT,
        "image_extractor": image_extractor.SYSTEM_PROMPT,
        "judge": judge.SYSTEM_PROMPT,
        "judge-classification": judge.CLASSIFICATION_SYSTEM_PROMPT,
        "judge-correctness": judge.CORRECTNESS_SYSTEM_PROMPT,
        "arbiter": arbiter.ARBITER_SYSTEM_PROMPT,
    }
