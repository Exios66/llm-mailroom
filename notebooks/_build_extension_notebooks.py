"""Generate notebooks 09–13 (thin walkthroughs over pipeline_lab / huggingface_lab / legalbench_lab)."""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

NB = Path(__file__).resolve().parent

BOOTSTRAP = '''import sys
from pathlib import Path

ROOT = Path.cwd()
while ROOT != ROOT.parent and not (ROOT / "pyproject.toml").exists():
    ROOT = ROOT.parent
assert (ROOT / "notebooks" / "pipeline_lab.py").exists(), (
    f"llm-mailroom repo root not found above {Path.cwd()}"
)
sys.path.insert(0, str(ROOT / "notebooks"))
sys.path.insert(0, str(ROOT / "src"))
'''

META = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {
        "name": "python",
        "file_extension": ".py",
        "mimetype": "text/x-python",
        "pygments_lexer": "ipython3",
        "nbconvert_exporter": "python",
    },
}


def md(src: str) -> dict:
    return nbf.v4.new_markdown_cell(src.strip() + "\n")


def code(src: str) -> dict:
    return nbf.v4.new_code_cell(src.strip() + "\n")


def write(name: str, cells: list) -> None:
    nb = nbf.v4.new_notebook(cells=cells, metadata=META)
    path = NB / name
    nbf.write(nb, path)
    print("wrote", path)


def nb09() -> None:
    write("09_all_specialists.ipynb", [
        md("""# 09 · All specialists — one run per document class

Every taxonomy class through the **real** graph: sorter → the matching
specialist → report → catalog → archive. Seven documents, seven specialists,
seven extraction schemas.

**What you'll see:** a per-class table of the specialist that dispatched, the
node path the router actually took, the stage that landed, and the extracted
keys. Contracts ride the LangChain specialist; the other six ride the legacy
`agents.*` specialists (the same split the production graph uses).

**Honesty label:** the graph, dispatch, schemas, bins, and catalog are REAL.
The LLMs are the test-suite mocks (`FakeLangChainLLM` for the LangChain path,
a scripted OpenAI client for the legacy specialists). Outputs are what the
pipeline produced under those mocks — OFFLINE, no API key.

Companion: `notebooks/pipeline_lab.py` (`CLASS_PACKS`, `run_all_classes`)."""),
        md("## Setup"),
        code(BOOTSTRAP + """
import pipeline_lab as lab
lab.quiet_logs()
"""),
        md("""## The roster this notebook exercises

Each pack is a document + a canned classification + a canned extraction that
matches that class's Pydantic schema. `run_all_classes` scripts every legacy
specialist marker and runs one document per class."""),
        code("""packs = lab.CLASS_PACKS
print(f"{len(packs)} classes:")
for key, pack in packs.items():
    print(f"  {key:22s} {pack['specialist']:32s} path={pack['path']:9s} file={pack['filename']}")
"""),
        md("""## Run — one document per class

Each class gets its own `matter_id` so a shared field name (`effective_date`
on both contract and corporate_record) cannot look like a contradiction.
Same-class conflict is notebook 10; mixed matters are notebook 07."""),
        code("""env = lab.open_sandbox()
rows = lab.run_all_classes(env)
print(f"{'class':22s} {'stage':10s} {'keys':4s}  path")
print("-" * 88)
for row in rows:
    print(f"{row['doc_class']:22s} {str(row['stage']):10s} {len(row['extracted_keys']):4d}  {' → '.join(row['path'])}")
print()
print("all archived:" , all(r["stage"] == "archived" for r in rows))
print("all seven classes dispatched:", [r["doc_class"] for r in rows])
"""),
        md("""## What each specialist wrote

The extracted payload is the specialist's schema, not a generic bag of
fields — `claimed_amount: 0.0` on the insurance FNOL is a real value (see
notebook 10), not an empty extraction."""),
        code("""arts = lab.artifacts(env["base_dir"])
docs = (arts.get("catalog") or {}).get("documents")
if docs:
    cols = docs["columns"]
    i_type = cols.index("doc_type")
    i_data = cols.index("extracted_data")
    import json
    for rec in docs["rows"]:
        payload = rec[i_data]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        keys = sorted(k for k in (payload or {}) if not str(k).startswith("_"))
        print(f"{rec[i_type]:22s} {keys}")
lab.close_sandbox(env)
"""),
        md("""## Where to go next

- **10 edge_cases** — unknown type, missing CUAD subtype, $0 amounts, schema-invalid extract, same-class conflict → Boss
- **07 multi_document_matters** — several classes under one `matter_id`
- **11 huggingface_corpora** — the Lucius-Morningstar datasets these classes were published from"""),
    ])


def nb10() -> None:
    write("10_edge_cases.ipynb", [
        md("""# 10 · Edge cases — guards, zeros, conflicts, unknown types

The paths that are not the happy path: classification guardrails, empty-vs-zero
extraction, schema-invalid specialist output, unknown classes, and the Boss
conflict lane. Each scenario is a real graph run under the mock seam.

**What you'll see:** five (plus one) composed-path outcomes, with the router
destination and the state fields that made it so.

**Honesty label:** REAL routers/guards/graph; deterministic mocks for the
LLMs. OFFLINE. These are the same defects the pipeline-logic audit pinned
(`apply_classification_guard`, `_has_substantive_content` treating `0` as
content, same-class conflict)."""),
        md("## Setup"),
        code(BOOTSTRAP + """
import pipeline_lab as lab
lab.quiet_logs()
env = lab.open_sandbox()
lab.script_all_specialists(env["client"])
"""),
        md("""## 1. Unknown class → human review

A sorter hallucination (`zzz_unknown` at 0.98) must never auto-extract.
`after_classify` parks the document on the review siding."""),
        code("""r = lab.run_document(
    env, lab.DOC_CONTRACT, filename="unknown.txt",
    classification=lab.CLASSIFY_UNKNOWN, extraction=lab.EXTRACT_HIGH,
)
print("path:", " → ".join(lab.path_of(r["steps"])))
print("stage:", r["final"].get("stage"), " doc_type:", r["final"].get("doc_type"))
assert r["final"].get("stage") == "review"
"""),
        md("""## 2. Contract missing CUAD subtype → clamp → retry

A contract at 0.98 with no subtype used to auto-extract. The classification
guard now clamps confidence to 0.5 (below `low`), so the router spends the
re-classification pass instead of dispatching the specialist."""),
        code("""r = lab.run_document(
    env, lab.DOC_CONTRACT, filename="no_subtype.txt",
    classification=lab.CLASSIFY_CONTRACT_NO_SUBTYPE, extraction=lab.EXTRACT_HIGH,
)
print("path:", " → ".join(lab.path_of(r["steps"])))
print("classification_confidence:", r["final"].get("classification_confidence"))
print("classification_guardrail:", r["final"].get("classification_guardrail"))
print("stage:", r["final"].get("stage"))
"""),
        md("""## 3. Numeric zero is content — $0 claim still archives

A deductible-only FNOL with `claimed_amount: 0.0` is a real extraction, not an
empty one. `_has_substantive_content` treats zero as a value."""),
        code("""r = lab.run_document(
    env, lab.DOC_INSURANCE_CLAIM, filename="fnol_zero.txt",
    classification=lab.CLASSIFY_INSURANCE_HIGH,
    extraction=lab.INSURANCE_CLAIM_EXTRACTION,
)
print("path:", " → ".join(lab.path_of(r["steps"])))
print("stage:", r["final"].get("stage"))
print("claimed_amount:", (r["final"].get("extracted_data") or {}).get("claimed_amount"))
assert r["final"].get("stage") == "archived"
assert (r["final"].get("extracted_data") or {}).get("claimed_amount") == 0.0
"""),
        md("""## 4. Schema-invalid extraction → retry, not archive

`parties: 123` is not a list. The extraction guard clamps confidence so the
router retries instead of archiving garbage. After the retry budget the
document parks for review (the mock keeps returning the same bad shape)."""),
        code("""r = lab.run_document(
    env, lab.DOC_CONTRACT, filename="bad_schema.txt",
    classification=lab.CLASSIFY_CONTRACT_HIGH,
    extraction=lab.EXTRACT_SCHEMA_INVALID,
)
print("path:", " → ".join(lab.path_of(r["steps"])))
print("stage:", r["final"].get("stage"))
print("extraction_guardrail:", r["final"].get("extraction_guardrail"))
print("extraction_attempts:", r["final"].get("extraction_attempts"))
"""),
        md("""## 5. Same-class conflict → Boss

Two contracts in one matter, different `governing_law`. The second run
detects the contradiction and sends the document to the Boss (here the mock
Boss approves, so the run still archives after adjudication)."""),
        code("""lab.script_all_specialists(env["client"])
first = lab.run_document(
    env, lab.DOC_CONTRACT, filename="msa_de.txt", matter_id="LAB-CONFLICT",
    classification=lab.CLASSIFY_CONTRACT_HIGH,
    extraction={**lab.EXTRACT_HIGH, "governing_law": "Delaware", "confidence": 0.96},
)
print("first:", first["final"].get("stage"), first["final"].get("conflict_detected"))
second = lab.run_document(
    env, lab.DOC_CONTRACT, filename="msa_ny.txt", matter_id="LAB-CONFLICT",
    classification=lab.CLASSIFY_CONTRACT_HIGH,
    extraction={**lab.EXTRACT_HIGH, "governing_law": "New York", "confidence": 0.96},
)
print("second path:", " → ".join(lab.path_of(second["steps"])))
print("conflict_detected:", second["final"].get("conflict_detected"))
print("stage:", second["final"].get("stage"))
assert second["final"].get("conflict_detected") is True
assert "adjudicate-conflict" in lab.path_of(second["steps"])
"""),
        md("""## 6. Mixed-class shared field names are NOT a conflict

A bylaws `effective_date` next to an MSA `effective_date` in the same matter
is two documents. Conflict detection is same-class only."""),
        code("""mixed = lab.run_document(
    env, lab.DOC_CORPORATE_RECORD, filename="bylaws_same_matter.txt",
    matter_id="LAB-CONFLICT",
    classification=lab.CLASSIFY_CORPORATE_HIGH,
    extraction=lab.CORPORATE_RECORD_EXTRACTION,
)
print("path:", " → ".join(lab.path_of(mixed["steps"])))
print("conflict_detected:", mixed["final"].get("conflict_detected"))
print("stage:", mixed["final"].get("stage"))
assert mixed["final"].get("conflict_detected") is False
lab.close_sandbox(env)
"""),
        md("""## Where to go next

- **05 failure_recovery** — transient provider errors vs confidence budgets
- **04 human_in_the_loop** — what happens after the review siding
- **03 review_lanes** — Lane A / Lane B (judge, arbiter, bounded retry)"""),
    ])


def nb11() -> None:
    write("11_huggingface_corpora.ipynb", [
        md("""# 11 · Hugging Face corpora — Lucius-Morningstar

Navigate and explore the datasets the mailroom family publishes on
[Lucius-Morningstar](https://huggingface.co/Lucius-Morningstar): CUAD
contracts, LegalBench, docclass packs, Enron correspondence, and CMS
DE-SynPUF insurance claims — each mapped onto a mailroom document class.

**What you'll see:** the org catalog, first-row previews, substring search,
an equality filter, and one Hub row fed into the real pipeline (mock LLM).

**Honesty label:** default cells are OFFLINE. They read a committed Dataset
Viewer snapshot under `notebooks/fixtures/huggingface/` (dated in
`catalog.json`). Nothing below talks to the Hub unless you set
`MAILROOM_HF_LIVE=1` and run the marker-gated live cell at the bottom.
`legalbench-full` on the Hub is currently a stub viewer (placeholder rows);
notebook 12 is the real LegalBench suite.

Companion: `notebooks/huggingface_lab.py`."""),
        md("## Setup"),
        code(BOOTSTRAP + """
import huggingface_lab as hf
import pipeline_lab as lab
lab.quiet_logs()
print("snapshot date:", hf.catalog()["snapshot_date"])
print("source:", hf.catalog()["source"] if "source" in hf.catalog() else "offline-snapshot")
print("live requested:", hf.live_requested())
"""),
        md("""## The catalog

Seven datasets. `mailroom_classes` is the wiring back to `taxonomy.yaml`,
not a Hub tag — it is how this pipeline consumes the published surface."""),
        code("""cat = hf.catalog()
hf.show_catalog(cat["datasets"])
print()
print("org:", cat["org_url"])
"""),
        md("""## Preview — insurance claims (CMS DE-SynPUF)

Synthetic Medicare claims, labeled `insurance_claim`. No real PHI."""),
        code("""claims = hf.preview("Lucius-Morningstar/cms-desynpuf-insurance-claims", length=3)
hf.show_rows(claims, text_chars=140)
print("features:", claims.get("features"))
"""),
        md("## Preview — Enron correspondence (deduped)"),
        code("""enron = hf.preview("Lucius-Morningstar/enron-correspondence-dedup", length=3)
hf.show_rows(enron, text_chars=120)
"""),
        md("## Preview — CUAD full contracts (clause labels in `expected`)"),
        code("""cuad = hf.preview("Lucius-Morningstar/mailroom-cuad-contracts-full", length=2)
hf.show_rows(cuad, text_chars=140)
"""),
        md("""## Search the snapshot

Offline search is a substring over the committed first-row window — not the
full 247k Enron rows. The live cell at the bottom uses Dataset Viewer `/search`."""),
        code("""hits = hf.search("Lucius-Morningstar/enron-correspondence-dedup", "forecast")
print("query=forecast  hits=", len(hits["rows"]), " source=", hits["source"])
hf.show_rows(hits, text_chars=100)
"""),
        md("## Filter — insurance rows labeled `insurance_claim`"),
        code("""filt = hf.filter_rows(
    "Lucius-Morningstar/cms-desynpuf-insurance-claims",
    where="expected=insurance_claim",
)
print("hits:", len(filt["rows"]), "source:", filt["source"])
print("labels:", sorted({r.get("expected") for r in filt["rows"]}))
"""),
        md("""## Feed a Hub row into the pipeline

Take the first DE-SynPUF claim's `doc_text`, run it as an `insurance_claim`
through the real graph (mock specialist). The catalog join in
`dataset_browser` is the local-pilot analogue of this."""),
        code("""row = claims["rows"][0]
text = hf.row_to_doc_text(row)
print("filename:", row.get("filename"), " chars:", len(text))
env = lab.open_sandbox()
lab.script_all_specialists(env["client"])
run = lab.run_document(
    env, text[:4000], filename="hf_claim.txt",
    classification=lab.CLASSIFY_INSURANCE_HIGH,
    extraction=lab.INSURANCE_CLAIM_EXTRACTION,
)
print("path:", " → ".join(lab.path_of(run["steps"])))
print("stage:", run["final"].get("stage"), " type:", run["final"].get("doc_type"))
lab.close_sandbox(env)
"""),
        md("""## Docclass + CUAD + LegalBench at a glance

| dataset | mailroom use |
|---|---|
| `docclass-merged` / `docclass-pilot` | sorter training / classification experiments |
| `mailroom-cuad-contracts` | vision surface (page images) |
| `mailroom-cuad-contracts-full` | contract texts + CUAD clause labels |
| `legalbench-full` | Hub stub — use notebook 12 |
| `enron-correspondence-dedup` | correspondence specialist |
| `cms-desynpuf-insurance-claims` | insurance_claim specialist |

Local pilot samples (30 rows, no Hub) still live in `dataset_browser.ipynb`."""),
        md("""## Live Hub refresh (opt-in)

<!-- NB-OPT-IN-NETWORK: Dataset Viewer / Hub API; skipped unless MAILROOM_HF_LIVE=1 -->

Set `MAILROOM_HF_LIVE=1` (optional `HF_TOKEN` for higher rate limits) and
re-run to hit `https://datasets-server.huggingface.co` for a fresh catalog
and a live search. Default execution never takes this branch."""),
        code("""import os
if os.environ.get("MAILROOM_HF_LIVE", "").strip().lower() in ("1", "true", "yes", "on"):
    live_cat = hf.catalog(live=True)
    print("LIVE catalog source:", live_cat.get("source"), "n=", len(live_cat["datasets"]))
    live_hits = hf.search("Lucius-Morningstar/enron-correspondence-dedup", "forecast", live=True)
    print("LIVE search hits:", live_hits.get("num_rows_total"), "source:", live_hits.get("source"))
else:
    print("live cell skipped (MAILROOM_HF_LIVE not set) — offline snapshot used above.")
"""),
        md("""## Where to go next

- **12 legalbench** — the in-repo eval suite (binary QA + family classification)
- **13 vision_ingestion** — page images, the other CUAD surface
- **dataset_browser** — the 30 local pilot samples + catalog overlay"""),
    ])


def nb12() -> None:
    write("12_legalbench.ipynb", [
        md("""# 12 · LegalBench — the evaluation suite beside the pipeline

LegalBench is a **second lens** on model quality: it does not sit inside the
13-node graph. Two task families run against locally mirrored CUAD corpora
(or, here, a miniature fixture so the notebook is network-free).

**What you'll see:** the live task registry, a mock `contract_qa` run, a mock
`family_classification` run, and how that relates to the Hub dataset
`Lucius-Morningstar/legalbench-full` (currently a stub viewer).

**Honesty label:** `run_mini` uses `legalbench.runner.run_task(..., mock=True)`
on a 2-contract fixture (`notebooks/fixtures/legalbench/`). Scores are
`mock/mock-legalbench`, not a real model. The full 20,910-question corpus
lives at `data/cuad/` after `scripts/fetch_full_cuad.py`. OFFLINE."""),
        md("## Setup"),
        code(BOOTSTRAP + """
import legalbench_lab as lb
import huggingface_lab as hf
print("mini CUAD:", lb.MINI_CUAD.exists(), lb.MINI_CUAD)
print("mini contracts dir:", lb.MINI_CONTRACTS.exists())
"""),
        md("## Task registry (live `legalbench.tasks`)"),
        code("""for t in lb.task_table():
    print(f"{t['id']:24s} kind={t['kind']}")
    print(f"{'':24s} prompt={t['prompt_version']}  classes={t['n_classes']}  e.g. {t['classes_head']}")
"""),
        md("""## Mock contract_qa on the miniature corpus

6 (contract × clause-category) questions, deterministic fake model, no
Langfuse. Scoring is local and never LLM-graded."""),
        code("""qa = lb.run_mini("contract_qa", n=6, seed=1)
print("task:", qa["task"], "model:", qa["model"], "n:", qa["n"])
print("scores:", {k: qa["scores"][k] for k in list(qa["scores"])[:8]})
print("honesty:", qa["honesty"])
print()
for i, row in enumerate(qa["rows"], 1):
    mark = "ok" if row["correct"] else "miss"
    print(f"  {i}. {mark:4s} expected={row['expected']!r:5s} predicted={row['predicted']!r}")
"""),
        md("## Mock family_classification (25 CUAD families + other)"),
        code("""fam = lb.run_mini("family_classification", n=2, seed=3)
print("task:", fam["task"], "n:", fam["n"])
print("scores:", {k: fam["scores"].get(k) for k in ("accuracy", "accuracy_equiv", "macro_f1")})
for i, row in enumerate(fam["rows"], 1):
    print(f"  {i}. expected={row['expected']!r:16s} predicted={row['predicted']!r} correct={row['correct']}")
"""),
        md("""## Hub `legalbench-full` is not this suite

The published dataset currently has placeholder viewer rows. The real eval
is `python -m legalbench.cli --task contract_qa --n 30 --mock` against
`data/cuad/`."""),
        code("""stub = hf.preview("Lucius-Morningstar/legalbench-full")
print("hub rows:", len(stub.get("rows") or []), "features:", stub.get("features"))
print("first row:", (stub.get("rows") or [{}])[0])
print()
print("Use legalbench.cli for a real (still mockable) run; this notebook stays on the fixture.")
"""),
        md("""## Where to go next

- **11 huggingface_corpora** — CUAD + DE-SynPUF + Enron on the Hub
- **01 happy_path_run** — the pipeline this eval sits *beside*
- **08 observability_traces** — how a LegalBench run would look in Langfuse"""),
    ])


def nb13() -> None:
    write("13_vision_ingestion.ipynb", [
        md("""# 13 · Vision ingestion — page images on top of transcription

Vision is **additive**: every agent prompt still contains the full `doc_text`.
Page images are appended only for vision-capable models, bounded by
`vision.max_pages` (0 = all pages). This notebook renders a real one-page PDF
inside the sandbox and shows the data-URIs `llm.vision.render_pdf_pages`
produces — no LLM call.

**What you'll see:** live `vision:` config, `pipeline_uses_vision()`, a
reportlab PDF rasterized by PyMuPDF, and the guarantee that `doc_text` is
never dropped when images are attached.

**Honesty label:** REAL render path (`llm.vision`, PyMuPDF). No model is
invoked. The multimodal `_build_multimodal` assembly is shown by inspecting
helpers, not by spending tokens. OFFLINE."""),
        md("## Setup"),
        code(BOOTSTRAP + """
import pipeline_lab as lab
lab.quiet_logs()
from llm import vision as v
"""),
        md("## Live vision config (taxonomy.yaml + env overrides)"),
        code("""print("vision_enabled:       ", v.vision_enabled())
print("max_pages (0=all):    ", v.max_pages())
print("pipeline_uses_vision: ", v.pipeline_uses_vision())
print("qwen capable:         ", v.is_vision_capable("qwen/qwen3.7-flash"))
print("unknown model:        ", v.is_vision_capable("some-text-only-model"))
"""),
        md("""## Render a one-page PDF

`write_lab_pdf` drops a real PDF in the sandbox inbox. `render_pdf_pages`
returns `data:image/png;base64,...` URIs — the same payload the sorter /
specialist prompts append as `image_url` parts."""),
        code("""env = lab.open_sandbox()
pdf = lab.write_lab_pdf(env["base_dir"], lab.DOC_CONTRACT, filename="vision_msa.pdf")
print("pdf:", pdf.name, "bytes:", pdf.stat().st_size)
pages = v.render_pdf_pages(pdf, cap=2, dpi=72)
print("pages rendered:", len(pages))
for i, uri in enumerate(pages, 1):
    head, _, rest = uri.partition(",")
    print(f"  page {i}: {head}  payload_chars={len(rest)}  prefix={uri[:48]}…")
"""),
        md("""## Additive, never subtractive

A pipeline run on this PDF still stores transcription in `doc_text`. Images
ride alongside; a page cap never drops document text. (`cap<=0` renders every
page — the config `max_pages` only bounds the *image budget*.)"""),
        code("""lab.script_all_specialists(env["client"])
run = lab.run_document(
    env, lab.DOC_CONTRACT, filename="vision_text_twin.txt",
    classification=lab.CLASSIFY_CONTRACT_HIGH, extraction=lab.EXTRACT_HIGH,
)
print("text twin path:", " → ".join(lab.path_of(run["steps"])))
print("stage:", run["final"].get("stage"))
print("doc_text present:", bool(run["final"].get("doc_text")))
lab.close_sandbox(env)
"""),
        md("""## Where to go next

- **00 pipeline_anatomy** — which agents are vision-capable
- **11 huggingface_corpora** — `mailroom-cuad-contracts` is the image-folder CUAD surface
- **01 happy_path_run** — the text path this notebook sits on top of"""),
    ])


if __name__ == "__main__":
    nb09(); nb10(); nb11(); nb12(); nb13()
