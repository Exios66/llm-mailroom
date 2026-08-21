# PROVENANCE — graphify skill

This directory is a **vendored copy** of the Graphify agent skill, installed for
use by any coding agent working in this repository (house pattern:
`.opencode/skills/`). It was copied by the hermes agent on **2026-08-21** under
**KANBAN-065** ([issue #30](https://github.com/Exios66/llm-entity-extraction/issues/30)).

| Field | Value |
|---|---|
| Upstream project | [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) |
| Source ref | default branch `v8`, commit `b2cd36267456c166788c95be6e68574064a92a42` ("chore: bump to 0.9.48", 2026-08-20) |
| Files copied | `graphify/skill-opencode.md` → `SKILL.md` (verbatim); `graphify/skills/opencode/references/*.md` → `references/` (verbatim, 8 files) |
| License | Dual Apache-2.0 / MIT (upstream). This vendored copy carries the upstream license terms; nothing here modifies them. |
| Local changes | NONE to the copied files. This PROVENANCE file is the only addition. |

## Why the opencode variant

Both governed repos keep their house skills under `.opencode/skills/<name>/`
(langfuse, braintrust, langchain-\*, openrouter-\*), so the opencode platform
variant matches the existing convention. The variant differences between
platforms are dispatch mechanics only; the graphify workflows (build / query /
path / explain / update) are identical.

## How to re-sync with upstream

```bash
git clone --depth 1 https://github.com/Graphify-Labs/graphify /tmp/graphify-upstream
diff -rq /tmp/graphify-upstream/graphify/skill-opencode.md SKILL.md
diff -rq /tmp/graphify-upstream/graphify/skills/opencode/references references/
# after any intentional update, refresh THIS file's "Source ref" row + date
```

The sibling repo `llm-mailroom` carries the same vendored skill at
`.opencode/skills/graphify/`; keep both copies byte-identical when updating.
A network-free consistency test pins this (entity: `tests/test_graphify_skill.py`,
mailroom: `src/tests/test_graphify_skill.py`).

## What graphify needs at runtime

Nothing is required just because these files exist. To actually run `/graphify`,
install the CLI once (`uv tool install graphifyy` or `pipx install graphifyy`)
and build the graph (`graphify .` → `graphify-out/graph.json`). Building is a
local, deterministic AST parse — no LLM calls, no API keys, no vector store.
