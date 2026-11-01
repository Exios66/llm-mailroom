# Decision reviewer calibration: `openai/gpt-6-luna-decisions` (SAND-085, issue #208)

2026-11-01, signed JB. This is the calibration of the decision reviewer on the
**Decisions-API model**. It supersedes every cutoff in
`reviewer-calibration-luna.md`, whose runs used the chat model
`openai/gpt-6-luna` by mistake (see "What went wrong" below).

## The model and how it is asked

`openai/gpt-6-luna-decisions` is GPT-6 Luna served through OpenRouter's
Decisions API (`POST https://openrouter.ai/api/alpha/decisions`). It has no
chat route. A request carries the document as `state` and a set of typed
questions; the answer gives a probability for every option and has no output
tokens. Price: $0.10 per 1M input tokens, no output charge.

One request per document asks:

- `doc_type`: a pick-one question over the five classes and `unknown`. Each
  option's criterion carries the class definition and the corpus conventions.
  The cutoff applies to the probability of the chosen class.
- One subclass question per class (`subclass__<class>`), each with a
  `not_this_class` option. Without it the model refuses a question none of
  whose options fits (a 502 "refused to answer"). Only the chosen class's
  answer is used.
- For correspondence, seven yes/no marker questions (`marker__correspondence__*`)
  instead of a pick-one question. The corpus labels correspondence subclasses
  by keyword markers in a fixed precedence order, which a pick-one question
  cannot express, so code applies the order to the marker probabilities.

The document is billed once per request: on a 29k-character contract, one
question cost 7,035 input tokens and six questions 8,946. The state is cut at
120,000 characters, because on the train split every merger's conversion terms
start before character 103,101 and the mergers make up 76% of the full-text
tokens.

Question sets: `config/decisions/reviewer_decisions_v1.yaml` (first run) and
`reviewer_decisions_v2.yaml` (after the improvement loop). Preflight freezes the
set's content and sha256 in the run lock.

## Runs

| Run | Documents | Question set | Class accuracy | Subclass accuracy | Input tokens | Cost |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| `reviewer-calibration-luna-decisions` (train) | 735 | v1 | 98.4% (723) | 78.0% | ~7.1M | $0.71 |
| `reviewer-calibration-luna-decisions-v2` (train, in-sample) | 735 | v2 | **100% (735)** | 82.6% | 8.55M | $0.69 |
| `reviewer-validation-luna-decisions-v2-test` (held-out test) | 323 | v2 | **98.5% (318)** | 83.3% | 2.38M | $0.24 |
| `pipeline-luna-decisions-qwen-smoke` (full pipeline) | 10 | v2 | 10/10 | — | — | $0.10 |

The 735 train documents are the pilot's balanced draw (dataset sha256
`1975326bfa88`). Latency was p50 0.5 s and p95 0.9 s per document at
concurrency 16, with no failures.

## Cutoffs (v2, target 95%, five-fold cross-fit, Wilson 95% interval)

| Class | Cutoff | Held-out accuracy | 95% interval | Accepted |
| --- | ---: | ---: | --- | ---: |
| contract | 0.78 | 100% | 97.5–100% | 99.3% |
| corporate_record | 0.96 | 100% | 97.5–100% | 99.3% |
| correspondence | 0.64 | 100% | 97.5–100% | 98.7% |
| insurance_claim | 0.70 | 100% | 97.5–100% | 98.7% |
| merger_agreement | 0.99 | 100% | 97.2–100% | 99.3% |

File: `reviewer-calibration-luna-decisions-v2.json`. On v1, correspondence
missed the target (lower bound 91.8%) because of the First Notice of Loss
errors below.

### Held-out test split, train cutoffs applied without re-tuning

| | Chat model, prompt v3 (superseded) | **Decisions model, v2** |
| --- | ---: | ---: |
| Class accuracy | 98.8% | 98.5% |
| Routed with no person | 98.8% | 98.5% (318 of 323) |
| Misrouted among routed | 1 | 1 |
| Confidence of wrong answers | mostly 0.86–0.99 | 0.58, 0.62, 0.85, 1.0, 1.0 |

The single misroute in both is the "LIMITED POWER OF ATTORNEY" labelled
contract / Service; the reviewer calls it corporate_record / powers_of_attorney
at 1.0, which reads as a label error. The other wrong answer at 1.0 is an
accounting standard (EITF 96-13) labelled correspondence / memo, which the
reviewer calls unknown; unknown is not a decision class, so it parks.

**The point of the Decisions model is the confidence.** The chat model wrote
its own confidence and put most wrong answers at 0.86–0.99, where no cutoff
can separate them. The Decisions model's probabilities put every v1 train
error at 0.84 or below and three of five test errors below 0.86, so a cutoff
parks them instead of routing them.

## Improvement loop (v1 → v2)

v1's 12 class errors on the train draw, all at probability 0.52–0.84:

| Error | Count | v2 change |
| --- | ---: | --- |
| insurance_claim (First Notice of Loss, property) → correspondence or unknown | 8 | FNOL forms named in the insurance_claim criterion; correspondence criterion excludes claim paperwork |
| corporate_record (untitled subsidiary table) → unknown | 2 | A bare table of company names and jurisdictions is a list of subsidiaries |
| merger_agreement ("Transaction Agreement" with merger subsidiaries) → contract | 2 | Transaction and business-combination agreements that merge through a Merger Sub are merger_agreement |

Subclass changes: a title that names one contract family decides it
(Supply, Cooperation, Content License, Affiliate), and the correspondence
markers became yes/no questions. Changes in subclass accuracy on the train draw:

| Subclass | v1 | v2 |
| --- | ---: | ---: |
| contract / collaboration | 1/6 | 6/6 |
| contract / supply | 1/6 | 5/6 |
| contract / affiliate | 2/5 | 5/5 |
| correspondence / demand | 4/21 | 15/21 |
| correspondence / memo | 12/21 | 18/21 |
| correspondence / notice | 10/21 | 14/21 |
| correspondence / meeting_request | 18/21 | 14/21 |

## What limits subclass accuracy now: the labels

1. **Merger "other" is not a consideration type.** All 49 train mergers
   labelled `other` are agreements with no MAUD "Type of Consideration"
   annotation. Every annotated merger's label equals its MAUD answer. So
   `other` means "unannotated", and no reader of the document can predict it.
   On the 86 annotated mergers the v2 reviewer is right on 84 (97.7%; v1: 83). Fix in the
   dataset: label these `unannotated` and leave them out of subclass scoring.
2. **Correspondence subclasses are keyword labels.** The dataset's
   `label_evidence` reads "demand markers", "notice forms", "ordinary email
   correspondence". An ordinary email that mentions demand letters is labelled
   demand. v2 asks for the markers directly; what remains is noise in the
   keyword labeller (e.g. an Evite invitation labelled email, a NYMEX
   "Notice No." memo labelled notice).
3. **Corporate-record label noise.** On the train draw, 9 "Certificate of
   Amendment" / "Articles of Amendment" documents are labelled
   board_resolution, bylaws documents are labelled articles_of_incorporation,
   and restated articles are labelled other. The reviewer's answers match the
   documents' own titles.

Excluding the merger `other` documents, v2's train subclass accuracy is
604/686 (88.0%).

## Pipeline integration (smoke, 10 documents)

`config/runs/pipeline-luna-decisions-qwen-smoke.yaml`: Qwen3.7 Flash on every
node, the Decisions reviewer on all five classes with the v2 cutoffs.

- All 10 documents classified correctly; 176 s wall for the run (10 at a time;
  the longest document, a 442k-token merger, took 173 s end to end).
- The reviewer accepted 9 at 0.98–1.0. A property claim at 0.65 (cutoff 0.70)
  was escalated to a person as `reviewer_low_confidence`, which is the
  routing the cutoff is for.
- The sorter and intake read each document in one call (`max_input_chars`
  600,000); only the extraction specialists chunk.

## What went wrong, and the guard against it

Every reviewer run before this report (the 735 pilot, prompts v1–v3, the
full-train and test runs, two smokes) used `openai/gpt-6-luna`, the plain chat
model, through the `api` backend. That backend runs the `sorter_reviewer` chat
agent and reads a confidence the model writes in its JSON. The Decisions
adapter had never been built. About $2.50 went to those runs. Their findings
carried into the question sets (class conventions, subclass rules, label
noise), but their cutoffs do not apply to the Decisions model.

Guards now in the code:

- `ReviewerSpec` refuses a Decisions model on any backend but `decisions`, a
  chat model on `decisions`, and the chat `api` backend unless
  `allow_chat_model: true` is set.
- Preflight refuses a paid `task: sorter_reviewer` run with no `reviewer:`
  block, and a Decisions calibration whose `engine.model` differs from the
  reviewer model.
- `decide()` refuses a non-Decisions model id.
- The Decisions model has no chat route, so a misconfigured chat call fails
  with a 400 and bills nothing.

Also fixed on the way: a Decisions call records 0 output tokens, and the
vendored usage recorder read `output_tokens or completion_tokens`, so every
call was dropped and the API spend cap could not see the run. Usage is now
recorded under the `prompt_tokens` / `completion_tokens` keys.

## Next

1. Dataset: relabel merger `other` as unannotated; review the corporate-record
   and power-of-attorney labels listed above.
2. The full train split (2,979 documents) on v2 to tighten the intervals:
   about 30M input tokens at the 120k cap, roughly $3.
3. Extraction review on the same model: yes/no "is this field supported by
   the text" questions per extracted field, each with its own cutoff.
   Dates and arithmetic stay with the LLM judge.
4. ModernBERT comparison on the same test split (issue #219).
