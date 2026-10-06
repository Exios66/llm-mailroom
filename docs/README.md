# Mailroom Documentation

<figure><img src="assets/fumi/fumi.gif" alt="Fumi, the Mailroom mascot, a chibi postal maid with Hoot the owl on her shoulder" width="192"><figcaption><p>Fumi and Hoot keep the mailroom running.</p></figcaption></figure>

**One place to learn the Mailroom constellation: the llm-mailroom pipeline and every repository built around it.**

The Mailroom reads legal and business documents, works out what each one is, pulls the important fields out of it, and files it in an archive with a tamper-evident audit trail. A team of specialist LLM agents does the work, one state machine per document.

That system is spread across more than a dozen repositories: the pipeline itself, a scoring library, an evaluation harness, corpus builders, a local sandbox, two visualizers, an ML classifier, and the monorepo that ties them together. This site is the front door to all of them.

## Where to start

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

## Related files

- [`../README.md`](https://github.com/Exios66/llm-mailroom/blob/main/README.md) — package overview
- `wiki/` — GitHub-wiki-only pages (not part of this site)
- `assets/` — images and diagrams
- [`constellation/maintaining.md`](constellation/maintaining.md) — how this site is built and kept current
