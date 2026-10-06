# mailroom-issues

**The constellation-wide issue hub. No code; only coordination.**

|            |                                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------- |
| Repository | [LLM-Mailroom-Services/mailroom-issues](https://github.com/LLM-Mailroom-Services/mailroom-issues) |
| Role       | Cross-repo issues, epics, RFCs and the label taxonomy                                             |

## What it does

Every bug, feature, task or design proposal that touches more than one repository, or needs an organization-level decision, is filed here. Implementation always lands in a sibling repository; the issue here tracks it.

It also keeps a few shared references: the [constellation map](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/CONSTELLATION.md), and the frozen v1 prompt reference for the sorter and five specialists under `docs/prompts/frozen-v1/`, with lineage in [`docs/PROMPTS.md`](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/PROMPTS.md).

## Filing an issue

| Template        | For                                  |
| --------------- | ------------------------------------ |
| Bug report      | Something is broken                  |
| Feature request | A new capability                     |
| Task / TODO     | A small, single-owner item           |
| Epic            | A multi-issue, cross-repo workstream |
| RFC             | A design proposal that needs review  |

Every issue gets four label families: `type/*`, `domain/*`, `priority/*`, `status/*`. Single-repo bugs belong in that repository instead; see [Governance](../../how-it-fits-together/governance.md#filing-an-issue-in-the-right-place).

## Its documentation

* [README](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/README.md)
* [docs/ROUTING.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/ROUTING.md) — where an issue should be filed
* [docs/LABELS.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/LABELS.md) — the label taxonomy
* [docs/LIFECYCLE.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/LIFECYCLE.md) — how an issue moves from triage to close
* [docs/CONSTELLATION.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/CONSTELLATION.md) — the repository map
* [docs/PROMPTS.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/PROMPTS.md) — prompt lineage and the frozen v1 set
* [reports/](https://github.com/LLM-Mailroom-Services/mailroom-issues/tree/main/reports) — exported Modal vs API / GPU reports (built from the sandbox hub). Gallery on this site: [sandbox visuals](local-mailroom-sandbox/local-mailroom-sandbox-visuals.md). Pages: [https://llm-mailroom-services.github.io/mailroom-issues/](https://llm-mailroom-services.github.io/mailroom-issues/)
