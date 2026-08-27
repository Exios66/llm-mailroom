# Project Agent Skills

Committed Cursor skills for **llm-mailroom**. Agents should discover these under
`.cursor/skills/*/SKILL.md` and prefer them over inventing parallel providers,
sinks, or scoring stacks.

Start with [mailroom-tool-router](mailroom-tool-router/SKILL.md), then open
exactly one specialty skill.

| Skill | Use for |
| --- | --- |
| [mailroom-tool-router](mailroom-tool-router/SKILL.md) | **Start here** — pick the right tool |
| [openrouter](openrouter/SKILL.md) | Production default LLM (`get_llm`) |
| [ollama](ollama/SKILL.md) | Local Ollama cutover |
| [modal](modal/SKILL.md) | Remote Modal vLLM (`mailroom-vllm`) |
| [langfuse](langfuse/SKILL.md) | Default tracing + The-Mailroom |
| [apache-phoenix](apache-phoenix/SKILL.md) | Local cost-free Phoenix fallback |
| [braintrust](braintrust/SKILL.md) | Opt-in hosted Braintrust |
| [huggingface](huggingface/SKILL.md) | Hub corpora / `run_hf_pilot.py` |
| [langgraph](langgraph/SKILL.md) | Graph, routing, HITL, checkpointer |
| [dojo-scoring](dojo-scoring/SKILL.md) | `llm-dojo-scoring` pin + suites |
| [legalbench](legalbench/SKILL.md) | LegalBench CLI (not pipeline ingest) |

Deeper vendored skills (CLI scripts, Langfuse docs, LangChain/LangGraph primers,
Graphify) stay under [`.opencode/skills/`](../../.opencode/skills/). Agent
instruction snippets (sorter/specialist prompt addenda) live under
`src/langchain_agents/skills/` — those are **not** Cursor Agent Skills.

Companion (offline sandbox): [Exios66/local-mailroom-sandbox#4](https://github.com/Exios66/local-mailroom-sandbox/pull/4).
