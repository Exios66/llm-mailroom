<div align="center">

# 📝 Prompt Templates

**Prompt templates for the scoring engine.**

</div>

---

## Purpose

Prompt templates used by the scoring engine for evaluation tasks.

## Structure

| Path | Contents |
|:---|:---|
| [`templates/`](templates/) | Template files |

## Usage

Templates are loaded automatically by the scoring engine.

## Prompt <-> schema alignment

`alignment.py` compares the field block of each production specialist template
(`- name (type): ...`) with the fields `schemas/doc_classes.py` scores. `alignment_report()`
returns, per specialist, `missing_from_prompt` and `extra_in_prompt` (`confidence` and
`reasoning` never count). Known gaps live in `data/alignment_gaps.json` with a reason each (none today: contracts and merger production v2 closed theirs);
`tests/behavior/test_prompt_alignment.py` fails on a new gap and on a recorded gap that is gone.
A production row with `source_repo: llm-dojo-scoring` is authored here and skipped by `scripts/sync_production_prompts.py`. After changing
the catalog, a template or the gap record, run
`uv run --locked python scripts/render_prompt_changelog.py --write` to refresh
[`docs/prompts/PROMPT_CHANGELOG.md`](../../docs/prompts/PROMPT_CHANGELOG.md).

## Related Files

- `../scoring/` — Core scoring logic
- `../` — Package root
