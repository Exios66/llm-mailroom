# Run reports

Measured GPU and specialist-grid results from [Exios66/local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox). Canonical write-ups, cards, and serving JSON live in that repo's `reports/`. Figures from those runs are on [Visuals](local-mailroom-sandbox-visuals.md). Snapshot as of the sandbox `main` tip used to build this page (2026-10).

Parent guide: [local-mailroom-sandbox](./). Dedicated manuals: [Documentation](local-mailroom-sandbox-docs.md).

The interactive hub is [`reports/dashboard/mailroom-reports.html`](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/dashboard/mailroom-reports.html) (tabs: Overview · Specialists · Serving · API models · Modal vs API · Classifier · Data quality). Cross-repo markdown + SVG copies are published from that hub into [mailroom-issues/reports](https://github.com/LLM-Mailroom-Services/mailroom-issues/tree/main/reports) and the static site [https://llm-mailroom-services.github.io/mailroom-issues/](https://llm-mailroom-services.github.io/mailroom-issues/).

## SAND-37 — specialist grid score and cost

Qwen3-8B-AWQ on vLLM, NVIDIA L4, Hub `Lucius-Morningstar/mailroom-dataset` @ `ed7576b6`. Start here: [SAND-37-MASTER-SCORE-COST-CARD.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/SAND-37-MASTER-SCORE-COST-CARD.md). Detail: [SAND-37-MASTER-APPENDIX.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/SAND-37-MASTER-APPENDIX.md). Index: [reports/SAND-37/README.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/README.md).

Key findings from the master card:

1. **2×L4 at C32 raises throughput +99% at +0.4% cost per document** on the same 250 documents (Experiment 2 → 3). Median latency rises ×1.4–1.8. GPU count and client concurrency changed together.
2. **Larger runs cost less per document.** n = 100 vs n = 50 cuts GPU cost per document 19% on the four unchanged specialists (Experiment 3 → 4); 1 of 400 failed (0.25%).
3. **Merger is the quality gap; the † settings narrow it.** MAUD accuracy 0.035 → 0.140 and coverage 23% → 69% on the same 50 agreements, at 4.5× GPU cost per agreement.

| Experiment | Posture        | GPUs | Concurrency |     Docs / class |
| ---------- | -------------- | ---: | ----------: | ---------------: |
| 1          | 1×L4 C8 n=20   |    1 |           8 |               20 |
| 2          | 1×L4 C8 n=50   |    1 |           8 |               50 |
| 3          | 2×L4 C32 n=50  |    2 |          32 |               50 |
| 4          | 2×L4 C32 n=100 |    2 |          32 | 100 (merger 50†) |

Pooled serving efficiency:

| Metric           |    Exp 1 |    Exp 2 |    Exp 3 |    Exp 4 |
| ---------------- | -------: | -------: | -------: | -------: |
| Error rate       |     3.0% |     2.8% |     2.0% |    0.22% |
| Documents / min  |     8.74 |    10.40 |    20.71 |    11.86 |
| Tokens / s / GPU |      812 |    1,002 |    1,013 |    1,662 |
| GPU $ / document | $0.00153 | $0.00128 | $0.00129 | $0.00225 |

Metered Modal session total across the four experiments: **$3.39** for 1,050 documents (busy-window GPU $1.81, 53% busy share). Per-cell cards: [SAND37\_L4\_cost\_cards.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/SAND37_L4_cost_cards.md) · record: [SAND37\_L4\_cost\_record.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/SAND37_L4_cost_record.md) · reader pack: [READER-REPORT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/SAND-37/READER-REPORT.md).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-quality.png" alt="SAND-37 posture comparison: extraction quality by specialist"><figcaption><p>SAND-37 quality comparison across 1×L4 and 2×L4 postures (from the master appendix).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-efficiency.png" alt="SAND-37 posture comparison: serving efficiency"><figcaption><p>SAND-37 efficiency comparison (throughput and GPU cost).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-latency-cost.png" alt="SAND-37 latency versus cost"><figcaption><p>SAND-37 latency versus cost.</p></figcaption></figure>

## SAND-032 — Qwen3-8B-AWQ knob ladder on Modal L4

Program summary: [QWEN3-L4-LADDER-SUMMARY.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/serving/QWEN3-L4-LADDER-SUMMARY.md). Ladder write-up: [SAND032-LADDER.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/serving/SAND-32/SAND032-LADDER.md). App name on those runs is `sandbox-vllm-sand032` (sandbox study app — not this pipeline's `mailroom-vllm`).

Frozen L5 vs L0 baseline (correspondence n=20, 1×L4): **−44% wall, −43% $/doc at equal quality**. Frozen engine: AWQ-marlin, `kv_cache_dtype=fp8`, thinking off, `max_num_seqs=16`, CUDA graphs `[1,2,4,8,16]`. Scale-out correspondence n=100: **2×L4 is 2.06× faster at flat $/doc**.

Largest serving lever: **set `max_inputs` = `max_num_seqs` per container** (Modal fills one container up to `max_inputs` before routing; 64 vs 32 turned a 2×L4 fleet into one hot replica).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-ladder.svg" alt="SAND-032 knob ladder small multiples"><figcaption><p>SAND-032 knob ladder (L0 → frozen L5).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-admission.svg" alt="SAND-032 admission: seqs16 fleet versus seqs32 balanced fleet"><figcaption><p>Admission: seqs16 fleet versus seqs32 balanced fleet.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-routing.svg" alt="SAND-032 correspondence n=100 wall time by fleet"><figcaption><p>Correspondence n=100 wall time by fleet.</p></figcaption></figure>

## Reports hub and published copies

| Artifact                                                                                                                                         | Role                                                                                                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| [`reports/README.md`](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/README.md)                                             | How the hub, figures, and mailroom-issues export are built                                                           |
| [`reports/dashboard/mailroom-reports.html`](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/dashboard/mailroom-reports.html) | Built hub (specialist, serving, API, ModernBERT)                                                                     |
| [COST-COMPARISON-MODAL-VS-API.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/reports/COST-COMPARISON-MODAL-VS-API.md)    | Modal vs API cost comparison (exported)                                                                              |
| [MODAL-VLLM-GPU-REPORT.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/reports/MODAL-VLLM-GPU-REPORT.md)                  | GPU economics (exported)                                                                                             |
| [MASTER-REPORT.md](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/reports/MASTER-REPORT.md)                                  | Master status (exported)                                                                                             |
| GitHub Pages                                                                                                                                     | [https://llm-mailroom-services.github.io/mailroom-issues/](https://llm-mailroom-services.github.io/mailroom-issues/) |

Rebuild (in a sandbox checkout, sibling checkouts for eval-environment + mailroom-ml):

```bash
python reports/dashboard/build_hub.py           # rebuild the page
python reports/dashboard/build_hub.py --check   # fail if stale
```

## Archive and other trees

| Path                                                                                                                                       | What it is                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| [archive/MODAL-RUNS-REPORT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/archive/MODAL-RUNS-REPORT.md)           | Earlier Modal run write-up                                       |
| [archive/RUN50-MODAL-HF-REPORT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/archive/RUN50-MODAL-HF-REPORT.md)   | Run-50 Hub report                                                |
| [archive/QWEN-FLASH-COST-REPORT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/archive/QWEN-FLASH-COST-REPORT.md) | Qwen Flash cost note                                             |
| [reports/SAND-32/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/reports/SAND-32)                                            | Per-class SAND-032 run folders                                   |
| [reports/modernbert/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/reports/modernbert)                                      | Redirect — canonical ModernBERT reports live in eval-environment |

## Related

* [Visuals](local-mailroom-sandbox-visuals.md)
* [Documentation](local-mailroom-sandbox-docs.md)
* Pipeline [Modal + vLLM](../../../pipeline-reference-llm-mailroom/deployment/modal-vllm.md) (`mailroom-vllm`, not the sandbox study app)
