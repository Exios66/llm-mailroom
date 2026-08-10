# Vendored LangChain Agents

This directory contains **vendored LangChain agents** adapted for the LLM-Mailroom pipeline. These agents originate from `github.com/Exios66/llm-entity-extraction` (verified against commit `3a03d5c`, 2026-08-10 — issue #10 alignment check: `CONTRACT_SUBTYPES`, `_SUBTYPE_ALIASES`, `SUBTYPE_EQUIVALENCES`, `SORTER_SCHEMA`, `DOC_CLASSES`, and the `sorter_v5`/`contracts_specialist_v11` prompts are byte-identical to upstream) and have been integrated with mailroom-specific plumbing (`MAILROOM PATCH` markers).

## Overview

Two of Mailroom's agents are built on LangChain rather than the native `BaseAgent` architecture:

1. **SorterAgent** (`sorter_agent.py`) — Document classification with contract subtype detection
2. **ContractsSpecialist** (`specialist_agents.py`) — Structured extraction for contracts

All other agents (Corporate Records, Due Diligence, Correspondence, Compliance, Court Opinions, Reporter, Boss, PDF Transcriber, Judge) use the native `agents/base.py:BaseAgent` contract.

## Why Vendored?

- **Proven evaluation history**: The Sorter (`sorter_v5`) and Contracts Specialist (`contracts_specialist_v11`) prompts have been validated against legal benchmarks (CUAD, MAUD).
- **LangChain structured output**: Uses `langchain-openai`'s `ChatOpenAI.with_structured_output()` for reliable JSON schema adherence.
- **Specialized preprocessing**: HEAD+TAIL windowing for long documents (contracts often have key terms at the beginning and end).

## Files

| File | Description |
|------|-------------|
| `base_agent.py` | Base class for vendored agents — wraps `ChatOpenAI` with mailroom plumbing (run-deadline checks, per-call usage accounting, pages/vision support). |
| `sorter_agent.py` | `SorterAgent` — classifies documents into `doc_type` + `contract_subtype` (25 CUAD families) with confidence + reasoning. Re-exported at `agents/sorter.py`. |
| `specialist_agents.py` | `ContractsSpecialist` — extracts structured contract data per `ContractExtraction` schema. Re-exported at `agents/contracts_specialist.py`. |
| `classifier.py` | Shared classification utilities (subtype normalization, confidence derivation). |
| `prompts.py` | **All prompts for vendored agents** — `sorter_v5`, `contracts_specialist_v11`, and their variants. These are **versioned, eval-validated prompts** that bypass `llm/prompts.py:get_managed_prompt` (they don't link to Langfuse prompt management). Generations are still auto-traced via the `langfuse.openai` SDK patch. |
| `env_utils.py` | Environment variable helpers for vendored agents. |
| `openrouter_utils.py` | OpenRouter-specific model resolution for vendored agents. |
| `mock.py` | Mock utilities for testing vendored agents. |

## Key Differences from Native Agents

| Aspect | Vendored (LangChain) | Native (`BaseAgent`) |
|--------|---------------------|---------------------|
| **Prompt management** | Hardcoded versioned prompts in `prompts.py` | Langfuse-managed (`mailroom-<agent>`) with local fallback |
| **Structured output** | `with_structured_output(PydanticModel)` | `_call_structured()` with `response_format={"type": "json_object"}` |
| **JSON token guarantee** | Handled by LangChain | Hardcoded in `BaseAgent._call_structured` (literal `json` token) |
| **LLM client** | `ChatOpenAI` from `langchain-openai` | Raw OpenAI client via `llm/client.py:get_llm()` |
| **Tracing** | Auto via `langfuse.openai` patch | Auto via `langfuse.openai` patch (same) |
| **Retry** | Custom in `base_agent.py` | `llm/retry.py:retry_chat_completion` |
| **Token caps** | Per-call in `base_agent.py` | Per-agent `max_tokens` in `taxonomy.yaml` |

## Integration Points

### SorterAgent (`agents/sorter.py`)
```python
from langchain_agents.sorter_agent import SorterAgent

# Re-exported with mailroom typing
class SorterAgent:
    def classify(self, doc_text: str, pages: list[str] | None = None) -> tuple[str, str | None, float, str]:
        # Returns (doc_type, contract_subtype, confidence, reasoning)
```

### ContractsSpecialist (`agents/contracts_specialist.py`)
```python
from langchain_agents.specialist_agents import ContractsSpecialist

# Re-exported with mailroom typing
class ContractsSpecialist:
    def extract(self, doc_text: str, pages: list[str] | None = None, handoff_context: dict | None = None) -> dict:
        # Returns extraction dict matching ContractExtraction schema
```

## Adding a New Vendored Agent

1. Add the agent class in `specialist_agents.py` (or new file) extending `base_agent.py:VendoredBaseAgent`
2. Add the prompt template to `prompts.py` with a versioned name (e.g., `new_specialist_v1`)
3. Create a re-export wrapper in `agents/` that matches the native agent interface
4. Add dispatch entry in `graph/build_graph.py:_build_specialist_dispatch()`
5. Add config in `config/taxonomy.yaml` under `doc_classes` and `agents`
6. Register test fixtures in `tests/fixtures/<doc_type>/`

## Testing

Vendored agents are tested in `tests/test_agents/test_sorter.py` and `tests/test_agents/test_specialists.py`. Tests inject a mock `ChatOpenAI` client — see `tests/conftest.py` for the mock pattern.

## License

Vendored from `github.com/Exios66/llm-entity-extraction` (MIT License). The original repo's LICENSE is preserved in the commit history.