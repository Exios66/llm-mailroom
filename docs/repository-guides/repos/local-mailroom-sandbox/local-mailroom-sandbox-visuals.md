# Visuals

Charts and `sandbox watch` stills from [Exios66/local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox). Images are the tracked files in that repo (GitHub `main`); this page is the GitBook gallery. Narrative and tables: [Run reports](local-mailroom-sandbox-reports.md). Manuals: [Documentation](local-mailroom-sandbox-docs.md).

Parent guide: [local-mailroom-sandbox](./).

## Modal specialist performance (SAND-032 hub)

Deterministic SVGs from committed SAND-032 run reports (`hub_data.json`). Source captions: [MODAL-PERFORMANCE-VISUALS.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/reports/dashboard/MODAL-PERFORMANCE-VISUALS.md). PNGs (2×) live under [`reports/dashboard/viz/modal-performance/`](https://github.com/Exios66/local-mailroom-sandbox/tree/main/reports/dashboard/viz/modal-performance).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/cost-by-specialist-hardware.png" alt="Busy-window GPU cost per document by specialist and hardware"><figcaption><p>Cost per document by specialist and hardware.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/score-by-specialist-hardware.png" alt="Extraction score by specialist and hardware"><figcaption><p>Extraction score by specialist and hardware.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/score-heatmap.png" alt="Extraction score heatmap, specialist by hardware"><figcaption><p>Score heatmap (specialist × hardware).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/cost-vs-quality-scatter.png" alt="Scatter of busy dollars per document versus extraction score"><figcaption><p>Cost versus quality scatter.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/cost-doc-breakdown.png" alt="Stacked dollars per document: GPU busy, idle, and amortized cold boot"><figcaption><p>Cost breakdown: GPU busy wall, idle slots, amortized cold boot.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/modal-api-qwen8b-cost.png" alt="Modal L4 versus API Qwen3-8B cost per 1,000 documents"><figcaption><p>Modal versus API Qwen3-8B cost per 1,000 documents.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/modal-api-qwen8b-table.png" alt="Modal versus API Qwen3-8B comparison table"><figcaption><p>Modal versus API per class: $/doc, scores, break-evens.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/throughput-vs-concurrency.png" alt="Throughput versus client concurrency, faceted by specialist"><figcaption><p>Throughput versus concurrency.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/dashboard/viz/modal-performance/prefix-cache-over-time.png" alt="Prefix-cache hit versus wall time per replica"><figcaption><p>Prefix-cache hit over wall time (vLLM `/metrics`).</p></figcaption></figure>

## SAND-37 grid figures

From [`reports/SAND-37/figures/`](https://github.com/Exios66/local-mailroom-sandbox/tree/main/reports/SAND-37/figures) (written by `sandbox run card --master`).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-quality.png" alt="SAND-37 quality comparison across postures"><figcaption><p>Quality comparison.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-efficiency.png" alt="SAND-37 efficiency comparison across postures"><figcaption><p>Efficiency comparison.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-latency-cost.png" alt="SAND-37 latency versus cost"><figcaption><p>Latency versus cost.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-matched.png" alt="SAND-37 matched per-document comparison"><figcaption><p>Matched per-document comparison.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-tokens.png" alt="SAND-37 token composition comparison"><figcaption><p>Token composition.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/cmp-merger-dagger.png" alt="SAND-37 merger frozen versus dagger settings"><figcaption><p>Merger frozen versus † settings.</p></figcaption></figure>

Record charts (from the two-page master card):

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/record/1x-vs-2xL4-throughput.png" alt="Throughput, 1x versus 2x L4 on the same 250 documents"><figcaption><p>Throughput, 1× versus 2× L4 on the same 250 documents.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/record/2xL4-n50-vs-n100-cost.png" alt="Cost per 1,000 ok documents on 2x L4, n=50 versus n=100"><figcaption><p>Cost per 1,000 ok documents on 2× L4, n=50 versus n=100.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/SAND-37/figures/record/merger-frozen-vs-dagger.png" alt="Merger agreements, frozen versus dagger settings"><figcaption><p>Merger agreements, frozen versus † settings.</p></figcaption></figure>

## SAND-032 serving figures

From [`reports/serving/figures/`](https://github.com/Exios66/local-mailroom-sandbox/tree/main/reports/serving/figures).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-ladder.svg" alt="SAND-032 knob ladder small multiples"><figcaption><p>Knob ladder small multiples.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-routing.svg" alt="Correspondence n=100 wall time by fleet"><figcaption><p>Correspondence n=100 wall time by fleet.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-admission.svg" alt="Wall time, seqs16 fleet versus seqs32 balanced fleet"><figcaption><p>seqs16 fleet versus seqs32 balanced fleet.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-v2-prompts.svg" alt="Production versus v2 prompt, paired"><figcaption><p>Production versus v2 prompt.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/reports/serving/figures/sand032-s6-sorter1000-per-class-f1.svg" alt="Sorter 1000-doc run, per-class F1"><figcaption><p>Sorter s6 (1000-doc) per-class F1.</p></figcaption></figure>

## `sandbox watch` stills

Captured operator UI from [`docs/assets/watch/`](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/assets/watch). Guide: [mailroom-themed-logging.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/pretty-logging/mailroom-themed-logging.md).

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/board-terminal.svg" alt="sandbox watch board in the terminal theme"><figcaption><p>Watch board, terminal theme.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/sorting-wide-color.svg" alt="sandbox watch sorting, wide color layout"><figcaption><p>Sorting, wide color layout.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/dispatch-roles.svg" alt="sandbox watch dispatch roles"><figcaption><p>Dispatch roles.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/stopped-scorecard.svg" alt="sandbox watch stopped scorecard"><figcaption><p>Stopped scorecard.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/cold-boot-engine-ready.svg" alt="sandbox watch cold boot, engine ready"><figcaption><p>Cold boot: engine ready.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/local-mailroom-sandbox/main/docs/assets/watch/blink-owl.svg" alt="sandbox watch blinking owl"><figcaption><p>Watch owl.</p></figcaption></figure>

## Related

* [Run reports](local-mailroom-sandbox-reports.md)
* [Documentation](local-mailroom-sandbox-docs.md)
* [mailroom-issues reports/viz](https://github.com/LLM-Mailroom-Services/mailroom-issues/tree/main/reports/viz) (exported 2× PNGs for slides)
* Pipeline [Modal + vLLM](../../../pipeline-reference-llm-mailroom/deployment/modal-vllm.md)
