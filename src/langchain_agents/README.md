# Vendored LangChain Agents

This directory contains **vendored LangChain agents** adapted for the LLM-Mailroom pipeline. These agents originate from `github.com/Exios66/llm-entity-extraction` (verified against commit `3a03d5c`, 2026-08-10 — issue #10 alignment check: `CONTRACT_SUBTYPES`, `_SUBTYPE_ALIASES`, `SUBTYPE_EQUIVALENCES`, `SORTER_SCHEMA`, `DOC_CLASSES`, and the `sorter_v5`/`contracts_specialist_v11` prompts are byte-identical to upstream) and have been integrated with mailroom-specific plumbing (`MAILROOM PATCH` markers).

## What's here

Two of Mailroom's agents are built on LangChain rather than the native `BaseAgent` architecture; everything else in `agents/` is native:

1. **SorterAgent** (`sorter_agent.py`) — document classification with contract subtype detection (25 CUAD families). Re-exported at `agents/sorter.py`.
2. **ContractsSpecialist** (`specialist_agents.py`) — structured extraction for contracts. Re-exported at `agents/contracts_specialist.py`.

Why vendored: the `sorter_v5` / `contracts_specialist_v11` prompts are eval-validated against legal benchmarks (CUAD, MAUD); LangChain's `with_structured_output()` gives reliable schema adherence; HEAD+TAIL windowing handles long contracts.

## Files

| File | Description |
|------|-------------|
| `base_agent.py` | Base class for vendored agents — wraps `ChatOpenAI` with mailroom plumbing (run-deadline checks, per-call usage accounting, pages/vision support). |
| `sorter_agent.py` | `SorterAgent` — `doc_type` + `contract_subtype` + confidence + reasoning. |
| `specialist_agents.py` | `ContractsSpecialist` — `ContractExtraction`-shaped dicts. |
| `classifier.py` | Shared classification utilities (subtype normalization, confidence derivation). |
| `prompts.py` | Versioned prompts (`sorter_v5`, `contracts_specialist_v11`) — bypass `llm/prompts.py:get_managed_prompt` (no Langfuse prompt linking); generations still auto-traced via the `langfuse.openai` patch. |
| `env_utils.py` | Environment variable helpers for vendored agents. |
| `openrouter_utils.py` | OpenRouter-specific model resolution. |
| `mock.py` | Mock utilities for testing vendored agents. |

The one behavioral difference from native agents: prompts are hardcoded/versioned here instead of Langfuse-managed (`mailroom-<agent>`). Structured output, tracing, and retry behavior otherwise match the native path — see `agents/README.md` and `docs/agents.md` for the full architecture.

## Adding a New Vendored Agent

1. Add the agent class extending `base_agent.py:VendoredBaseAgent`
2. Add the versioned prompt template to `prompts.py`
3. Create a re-export wrapper in `agents/` matching the native agent interface
4. Add a dispatch entry in `graph/build_graph.py:_build_specialist_dispatch()`
5. Add config in `config/taxonomy.yaml` under `doc_classes` and `agents`
6. Register test fixtures in `tests/fixtures/<doc_type>/`

## Testing

Vendored agents are tested in `tests/test_agents/test_sorter.py` and `tests/test_agents/test_specialists.py` with a mock `ChatOpenAI` (see `tests/conftest.py`).

## License

Vendored from `github.com/Exios66/llm-entity-extraction` (MIT License). The original repo's LICENSE is preserved in the commit history.
