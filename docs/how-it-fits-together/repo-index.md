# Repository index

Every repository connected to the Mailroom, including copies and earlier attempts, with where to do work. When in doubt, develop in the monorepo and release through the `Exios66/*` repository.

Status snapshot: 2026-10-06. "Mirror" and "earlier monorepo" labels below are inferred from each repository's README and commit history; confirm with the owner before archiving anything.

## Active repositories

| Repository                                                                                          | Layer              | Develop in                                       | Guide                                                               |
| --------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------ | ------------------------------------------------------------------- |
| [LLM-Mailroom-Services/Digital-Mailroom](https://github.com/LLM-Mailroom-Services/Digital-Mailroom) | Hub monorepo       | here                                             | [guide](../repository-guides/repos/digital-mailroom.md)             |
| [LLM-Mailroom-Services/mailroom-issues](https://github.com/LLM-Mailroom-Services/mailroom-issues)   | Issue hub          | here                                             | [guide](../repository-guides/repos/mailroom-issues.md)              |
| [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom)                                     | Pipeline           | monorepo `packages/llm-mailroom`                 | [guide](../repository-guides/repos/llm-mailroom.md)                 |
| [Exios66/llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring)                             | Scoring            | monorepo `packages/llm-dojo-scoring`             | [guide](../repository-guides/repos/llm-dojo-scoring.md)             |
| [Exios66/llm-entity-extraction](https://github.com/Exios66/llm-entity-extraction)                   | Prompt experiments | monorepo `packages/llm-entity-extraction`        | [guide](../repository-guides/repos/llm-entity-extraction.md)        |
| [Exios66/local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox)                 | Local sandbox      | monorepo `packages/local-mailroom-sandbox`       | [guide](../repository-guides/repos/local-mailroom-sandbox/)         |
| [Exios66/The-Mailroom](https://github.com/Exios66/The-Mailroom)                                     | Visualizer         | monorepo `packages/The-Mailroom`                 | [guide](../repository-guides/repos/the-mailroom.md)                 |
| [Exios66/agent-mailroom](https://github.com/Exios66/agent-mailroom)                                 | Walking floor      | monorepo `packages/agent-mailroom`               | [guide](../repository-guides/repos/agent-mailroom.md)               |
| [Exios66/llm-mailroom-graph](https://github.com/Exios66/llm-mailroom-graph)                         | Knowledge graph    | monorepo `packages/llm-mailroom-graph`           | [guide](../repository-guides/repos/llm-mailroom-graph.md)           |
| [Exios66/Mailroom-Corpus-EDA](https://github.com/Exios66/Mailroom-Corpus-EDA)                       | Corpus EDA + Hub   | monorepo `packages/mailroom-corpus-eda`          | [guide](../repository-guides/repos/mailroom-corpus-eda.md)          |
| [Exios66/Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment)     | Corpus feed        | monorepo `packages/Enron-Evaluation-Environment` | [guide](../repository-guides/repos/enron-evaluation-environment.md) |
| [Exios66/claims-data-eda](https://github.com/Exios66/claims-data-eda)                               | Corpus feed        | monorepo `packages/claims-data-eda`              | [guide](../repository-guides/repos/claims-data-eda.md)              |
| [LLM-Mailroom-Services/eval-environment](https://github.com/LLM-Mailroom-Services/eval-environment) | Evaluation         | here                                             | [guide](../repository-guides/repos/eval-environment.md)             |
| [LLM-Mailroom-Services/mailroom-ml](https://github.com/LLM-Mailroom-Services/mailroom-ml)           | ML classifier      | here                                             | [guide](../repository-guides/repos/mailroom-ml.md)                  |
| [Exios66/atticus-investigation](https://github.com/Exios66/atticus-investigation)                   | Adjacent eval      | here                                             | [guide](../repository-guides/repos/atticus-investigation.md)        |
| [LLM-Mailroom-Services/.github](https://github.com/LLM-Mailroom-Services/.github)                   | Org profile        | here                                             | —                                                                   |

## Organization copies and forks

These live under the `LLM-Mailroom-Services` organization and track an `Exios66/*` repository or the monorepo. Do not start new work in them unless the owner says otherwise.

| Repository                                                                                                                    | Copy of                | Notes                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------- | ---------------------- | --------------------------------------------------------------------- |
| [LLM-Mailroom-Services/Mailroom-Pipeline](https://github.com/LLM-Mailroom-Services/Mailroom-Pipeline)                         | llm-mailroom           | Fork; commits are "monorepo propagation" pushes from Digital-Mailroom |
| [LLM-Mailroom-Services/Entity-Extraction-Experiments](https://github.com/LLM-Mailroom-Services/Entity-Extraction-Experiments) | llm-entity-extraction  | Organization copy listed in the org profile                           |
| [LLM-Mailroom-Services/Mailroom-Corpus](https://github.com/LLM-Mailroom-Services/Mailroom-Corpus)                             | Mailroom-Corpus-EDA    | Fork                                                                  |
| [LLM-Mailroom-Services/mailroom-sandbox](https://github.com/LLM-Mailroom-Services/mailroom-sandbox)                           | local-mailroom-sandbox | Fork                                                                  |
| [Exios66/Digital-Mailroom](https://github.com/Exios66/Digital-Mailroom)                                                       | Digital-Mailroom       | Personal fork of the hub                                              |

## Earlier monorepos and derived sites

| Repository                                                                            | Status                                                                                        |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| [Exios66/mailroom-hub](https://github.com/Exios66/mailroom-hub)                       | First monorepo (2026-08-30). Superseded by Digital-Mailroom.                                  |
| [Exios66/mailroom-dev](https://github.com/Exios66/mailroom-dev)                       | Later monorepo with the same layout; last updated 2026-09-26. Superseded by Digital-Mailroom. |
| LLM-Mailroom-Services/mailroom-ops                                                    | HUB-era corporate monorepo; archived, per the org profile.                                    |
| [Exios66/mailroom-dev-graph](https://github.com/Exios66/mailroom-dev-graph)           | Knowledge graph of mailroom-dev.                                                              |
| [llm-entity-extraction-graph](https://exios66.github.io/llm-entity-extraction-graph/) | Knowledge graph of the prompt experiment loop.                                                |

## Hosted surfaces

| Surface                 | URL                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| Dispatch Board          | [https://digital-mailroom-theta.vercel.app](https://digital-mailroom-theta.vercel.app)                         |
| eval-environment viewer | [https://eval-environment.vercel.app](https://eval-environment.vercel.app)                                     |
| Experiment log site     | [https://exios66.github.io/llm-entity-extraction/](https://exios66.github.io/llm-entity-extraction/)           |
| The-Mailroom            | [https://exios66.github.io/The-Mailroom/](https://exios66.github.io/The-Mailroom/)                             |
| Producer Space          | [https://lucius-morningstar-mailroom-producer.hf.space](https://lucius-morningstar-mailroom-producer.hf.space) |
| Hugging Face datasets   | [https://huggingface.co/Lucius-Morningstar](https://huggingface.co/Lucius-Morningstar)                         |
