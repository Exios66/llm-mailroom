# The LLM-Mailroom

<table>
<tr>
<td width="80" valign="middle">
<img src="assets/fumi/fumi.gif" alt="Fumi, the llm-mailroom mascot: a chibi postal maid with a mail satchel and Hoot the owl on her shoulder" width="64">
</td>
<td valign="middle">

**The LLM-Mailroom** — Fumi (文, "letter")

</td>
</tr>
</table>

<figure><img src="assets/banner.png" alt="Mailroom — a great horned owl postal worker sorting wax-sealed legal documents into bins by lamplight"><figcaption><p>The LLM-Mailroom masthead: the night-shift owl at the sorting desk.</p></figcaption></figure>

**A multi-agent pipeline that ingests, classifies, extracts, and archives legal documents — with a full audit trail.**

One LangGraph state machine per document. Specialist LLM agents per document class. Hash-chained audit log. Provider-agnostic LLM layer. Traced end-to-end.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Pipeline](https://img.shields.io/badge/LangGraph-13--node%20state%20machine-4C8CBF)](https://langchain-ai.github.io/langgraph/)
[![LLM layer](https://img.shields.io/badge/LLM-OpenRouter%20%7C%20Ollama%20%7C%20vLLM-8A2BE2)](https://github.com/Exios66/llm-mailroom#llm-providers)
[![Tracing](https://img.shields.io/badge/tracing-Langfuse%20%7C%20Braintrust%20%7C%20Phoenix-F5A623)](https://github.com/Exios66/llm-mailroom#observability)
[![Storage](https://img.shields.io/badge/storage-SQLite--first-lightgrey)](https://github.com/Exios66/llm-mailroom#quick-start)
[![Release](https://img.shields.io/badge/release-v0.7.1-2EA043)](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/releases/tag/v0.7.1)
[![Contributor](https://img.shields.io/badge/contributor-Exios66-blue)](https://github.com/Exios66)

This is the published home of [Mailroom Inc. Docs](https://mailroom-inc.gitbook.io/mailroom-inc.-docs/). It matches the repository landing page in `landing/` (banner, title, badges, Fumi in the header corner). GitBook strips scripts, so the idle mail-floor terminal stays on the static page; everything else below is the same walk-through.

```bash
# clone, install, then start the API (it embeds the inbox watcher)
git clone https://github.com/Exios66/llm-mailroom.git
cd llm-mailroom
pip install -e ".[dev]"
PYTHONPATH=src python -m api.main
```

## From inbox to archive

The happy path costs two LLM generations. Everything else is procedural, gated, or a human's call.

| Stop | What happens |
| :--- | :--- |
| **Intake** | The watcher claims the upload by atomic rename into `processing/`, transcribes it, and cleans it. Nothing is ever truncated. |
| **Classify** | The sorter picks one of five classes. At `0.97` or above it goes straight on; between `0.88` and `0.97` it goes to review. |
| **Extract** | The class specialist fills its schema. Guardrails check the JSON before anything moves forward. |
| **Verify** | Ambiguous extractions get a judge, then an arbiter. Conflicts go to the boss agent or a human. |
| **Archive** | A procedural report, a catalog row in SQLite, and an archive entry sealed into the hash-chained audit log. |

## What ships with it

| | |
| :--- | :--- |
| **Five document classes** | Contracts, merger agreements, corporate records, correspondence, and insurance claims, each with its own specialist. |
| **Any model provider** | OpenRouter, Ollama, or vLLM. Agents ask for a role; `taxonomy.yaml` decides the model. |
| **Traced end to end** | Langfuse, Braintrust, or a local Arize Phoenix. The pipeline runs fine with none of them. |
| **Evaluation built in** | A 23-sample pilot with ground truth, deterministic field scoring, LLM judges, and a LegalBench harness. |

## Where to start

**One place to learn the Mailroom constellation: the llm-mailroom pipeline and every repository built around it.**

The Mailroom reads legal and business documents, works out what each one is, pulls the important fields out of it, and files it in an archive with a tamper-evident audit trail. A team of specialist LLM agents does the work, one state machine per document.

That system is spread across more than a dozen repositories: the pipeline itself, a scoring library, an evaluation harness, corpus builders, a local sandbox, two visualizers, an ML classifier, and the monorepo that ties them together. This site is the front door to all of them.

| If you want to... | Read |
| :--- | :--- |
| Understand what the Mailroom is and which repo does what | [Overview](constellation/overview.md) |
| Get something running in the next ten minutes | [Getting started](constellation/getting-started.md) |
| See how data, prompts, scores and traces move between repos | [How the constellation fits together](constellation/architecture.md) |
| Look up a term like "Lane A", "STP" or "virtual member" | [Glossary](constellation/glossary.md) |
| Work on a specific repository | [Repository guides](constellation/repos/README.md) |
| Know which board, issue tracker or branch to use | [Governance and workflow](constellation/governance.md) |

## The pipeline reference

The pages below are the canonical documentation for the `llm-mailroom` pipeline package. They live in this repository's `docs/` folder and are also browsable locally with docmd (see the repository [README](https://github.com/Exios66/llm-mailroom#browsing-the-docs-locally)).

| Document | Description |
| :--- | :--- |
| [Architecture](architecture.md) | System design and the 13-node state machine |
| [Agents](agents.md) | LLM and procedural agent specifications |
| [Configuration](configuration.md) | `taxonomy.yaml`, environment variables, thresholds |
| [Gmail intake](gmail-intake.md) | Gmail intake route: upload guide, subject-line contract, pathways (HUB-037) |
| [API](api.md) | HTTP endpoint reference |
| [Deployment](deployment.md) | Production deployment |
| [Operational procedure](operational-procedure.md) | Day-to-day operating runbook |
| [Local models](local-models.md) | Local model integration and cutover |
| [Testing](testing.md) | Test organization |
| [Sister repositories](sister-repos.md) | The pipeline's own view of its neighbours |

## Meet Fumi

<figure><img src="assets/fumi/fumi.gif" alt="Fumi in her postal uniform while Hoot the owl blinks on her shoulder" width="192"><figcaption><p>Fumi (文, "letter") is the mailroom's head maid — a USPS-style carrier uniform, a mini cap on her headdress, a leather satchel, and Hoot checking postmarks from her shoulder. She is generated by <code>src/scripts/build_mascot.py</code> and shown in the header corner of this page and of <code>landing/</code>.</p></figcaption></figure>

## Related files

- [`../README.md`](https://github.com/Exios66/llm-mailroom/blob/main/README.md) — package overview
- [`../landing/`](https://github.com/Exios66/llm-mailroom/tree/main/landing) — standalone static landing page (idle mail-floor terminal; this GitBook home is the published port)
- `wiki/` — GitHub-wiki-only pages (not part of this site)
- `assets/` — images and diagrams
- [`constellation/maintaining.md`](constellation/maintaining.md) — how this site is built and kept current
