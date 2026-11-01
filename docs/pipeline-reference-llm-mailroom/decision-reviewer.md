# Decision reviewer

The decision reviewer gives an independent second classification of a document
and decides whether it routes to its extraction specialist without a person.
It runs at the `review_classify` node for every class configured as
`decision_only` in the taxonomy's `reviewer` block, at any sorter confidence.

## The model

The reviewer is a **Decisions-API model**, `openai/gpt-6-luna-decisions`, served
by OpenRouter at `POST https://openrouter.ai/api/alpha/decisions`. It is not a
chat model and has no chat-completions route:

| | Decisions model | Chat model (`openai/gpt-6-luna`) |
| --- | --- | --- |
| Input | `state` (the document) and typed `questions` | messages |
| Output | a probability for every option of every question | text; any confidence is self-reported |
| Price | $0.10 per 1M input tokens, no output charge | $0.10 in, $0.50 out per 1M |
| Context | 1.05M tokens | 1.05M tokens |

The probability is what makes a per-class cutoff work. Measured on the mailroom
dataset, the chat model put most of its wrong answers at a self-reported
0.86–0.99, where no cutoff separates them from right answers. The Decisions
model put every wrong answer on 735 train documents at 0.84 or below.

> Do not calibrate or run the reviewer on `openai/gpt-6-luna`. On 2026-11-01 a
> full calibration ran on the chat model by mistake; the sandbox now refuses that
> configuration (see "Guards").

## Integration

The graph never builds a backend. A host registers one with
`agents.decision_backend.register_decision_backend(backend)`; a backend is a
callable `backend(doc_text, doc_types, subclass_catalogs) -> DecisionAnswer`
(`pipeline.decision_review`). With nothing registered, `decision_only` classes
park for a person.

The sandbox's Decisions backend (`mailroom_sandbox.decision_backends.decisions_backend`)
sends one request per document:

- `doc_type`: a pick-one question over the live classes plus `unknown`. Each
  option's criterion carries the class definition and the corpus conventions.
  `DecisionAnswer.confidence` is the probability of the chosen class.
- `subclass__<class>`: one pick-one question per class, each with a
  `not_this_class` option. The model refuses a question with no fitting option,
  so every subclass question needs one. Only the chosen class's answer is used.
- Correspondence uses seven yes/no marker questions instead, and code takes the
  first marker present in the corpus's precedence order (attorney_demand,
  letter, press_release, memo, demand, notice, meeting_request; else email).

The document is billed once per request, however many questions it carries. The
state is cut at 120,000 characters, which covers every merger's conversion terms
on the train split.

The questions live in a versioned question set (sandbox
`config/decisions/reviewer_decisions_v2.yaml`, copied in
[reports/evaluations/decision-reviewer](../reports/evaluations/decision-reviewer/reviewer_decisions_v2.yaml)).
A run freezes the set's content and sha256 in its lock.

## Configuration (sandbox run spec)

```yaml
reviewer:
  backend: decisions
  model: openai/gpt-6-luna-decisions
  questions: reviewer_decisions_v2
  classes:            # cutoffs from the v2 calibration
    contract: 0.78
    merger_agreement: 0.99
    corporate_record: 0.96
    correspondence: 0.64
    insurance_claim: 0.70
```

A class at or above its cutoff that agrees with the sorter goes to `extract`.
Below the cutoff, or in disagreement with a medium or high sorter, the document
parks for human review (`reviewer_low_confidence`).

## Calibration results

Full report: [reviewer-calibration-luna-decisions.md](../reports/evaluations/decision-reviewer/reviewer-calibration-luna-decisions.md).

| | Result |
| --- | --- |
| Train (735 documents, question set v2, in-sample) | 100% class accuracy; all five classes meet 95% with a 97%+ lower bound |
| Held-out test split (323 documents, train cutoffs) | 98.5% routed with no person, 1 misroute (a likely label error) |
| Full pipeline smoke (10 documents, Qwen3.7 Flash elsewhere) | 10/10 classes correct; 9 accepted, 1 escalated at 0.65 < 0.70 |
| Cost | about $0.0009 per document at the 120k cap |

## Guards

- A Decisions model is accepted only on `backend: decisions`; a chat model is
  refused there.
- The chat-model `api` backend needs `allow_chat_model: true`.
- A paid `task: sorter_reviewer` run without a `reviewer:` block is refused at
  preflight.
- A Decisions call records 0 output tokens. `pipeline.limits.record_usage`
  reads `output_tokens or completion_tokens`, so a usage dict keyed
  `output_tokens: 0` is dropped. Record Decisions usage under `prompt_tokens` /
  `completion_tokens`.

## Recommended upstream changes

1. Move the Decisions backend from the sandbox into `agents/` as a first-class
   backend, with the question set beside the taxonomy.
2. Fix `record_usage` to treat an explicit `0` as a count (`is not None`, not `or`).
3. Sorter `max_input_chars`: the default 12,000 makes the sorter call once per
   12k window; one long contract took 44 sorter calls. With a 1M-context sorter model, read
   the whole document in one call.
4. Dataset: merger subclass `other` marks agreements with no MAUD "Type of
   Consideration" annotation, not a consideration type. Relabel it
   `unannotated` and leave it out of subclass scoring.
