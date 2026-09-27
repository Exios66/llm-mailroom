# Sister Repositories & the Mailroom Umbrella

llm-mailroom does not fly alone. It is the pipeline at the center of a small
constellation of governed repositories — each with its own repo, board
discipline, and release train — plus derived artifacts hosted elsewhere. Since
2026-08-30 the whole constellation also lives as **one monorepo**
([`Digital-Mailroom`](https://github.com/LLM-Mailroom-Services/Digital-Mailroom)): every family
repo is a git-subtree package under `packages/`, wired as a single `uv`
workspace, with the monorepo as the **source of truth for active
development**. This page maps who's who, what flows between them, and where
the canonical state of each lives. (Links verified 2026-08-23; monorepo
verified 2026-08-30.)

PLACEHOLDER_TRUNCATED