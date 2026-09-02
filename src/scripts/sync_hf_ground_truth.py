#!/usr/bin/env python3
"""Derive + publish the intent/subject_matter/keywords ground truth for
``Lucius-Morningstar/mailroom-corpus`` (ground_truth config).

The plan (KANBAN-07x): for ``corporate_record``, ``correspondence``, and
``insurance_claim`` the pipeline ground truth now includes three purpose/gist
fields — ``intent`` (one controlled label), ``subject_matter`` (one tight
grounded sentence), ``keywords`` (<=8 text-grounded terms). Those labels are
derived FROM the underlying Hub documents and pushed back to the dataset so
every successful run can be graded against them.

Workflow:

1. Fetch the pinned revision of ``mailroom-corpus`` — ``default`` config rows
   (``doc_text`` per filename) joined to the ``ground_truth`` config rows
   (``expected`` / ``expected_subclass`` + existing GT columns) on filename.
2. Keep only the three purpose-labeled classes; the rest of the table passes
   through unchanged.
3. Label each document:
   - ``--real``: the ``judge`` LLM labels intent/subject_matter/keywords from
     the document text (curated head+tail window), validated against the
     controlled ``INTENT_LABELS`` vocabulary.
   - ``--mock`` / ``--dry-run``: deterministic labeler (subclass-mapped intent,
     first-sentence subject, frequency keywords) — machinery test only, never
     published as real labels.
4. Emit the new ``ground_truth`` rows (existing columns + 3 new columns on the
   labeled classes) as a preview CSV under ``data/hf_gt/``.
5. ``--push`` uploads a new revision and (by default) updates the pinned
   ``FULL_CORPUS_REVISION`` in ``pipeline/hf_corpora.py`` so the next HF pilot
   consumes the enriched labels.

Usage:
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --check
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --dry-run --limit 20
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --mock --limit 20
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --real --limit 50
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --real --push
    PYTHONPATH=src python src/scripts/sync_hf_ground_truth.py --real --push --no-update-pin

``--real`` needs OPENROUTER_API_KEY (via get_llm); ``--push`` needs HF_TOKEN.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import structlog

logger = structlog.get_logger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

from pipeline.hf_corpora import (  # noqa: E402
    FULL_CORPUS_ID,
    FULL_CORPUS_REVISION,
)
from langchain_agents.doc_inventories import (  # noqa: E402
    INTENT_DESCRIPTIONS,
    INTENT_LABELS,
    normalize_intent,
)
from llm.client import get_llm  # noqa: E402
from llm.retry import retry_chat_completion  # noqa: E402

VIEWER_BASE = "https://datasets-server.huggingface.co"
LABELED_CLASSES: tuple[str, ...] = (
    "corporate_record",
    "correspondence",
    "insurance_claim",
)
MAX_SUBJECT_CHARS = 240
MAX_KEYWORDS = 8
MAX_TEXT_CHARS = 12000  # curated head+tail window sent to the labeler

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "subject_matter": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_KEYWORDS},
    },
    "required": ["intent", "subject_matter", "keywords"],
}

_STOPWORDS = frozenset(
    "a an the and or but if in on of to for with by from at as is are was were be been has have had it its this that these those not no yes will would shall may must per pursuant under section article upon into within between during after before any all each such other there here where which whom whose whose".split()
)


def head_tail_window(text: str, cap: int = MAX_TEXT_CHARS) -> str:
    """Curated head+tail window (like the sorter's budget truncation)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= cap:
        return text
    head = text[: cap * 3 // 4]
    tail = text[-cap // 4 :]
    return head + " \u2026[truncated]\u2026 " + tail


def clean_subject_matter(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip().strip('"').strip(".")
    if not text:
        return ""
    text = text[:MAX_SUBJECT_CHARS].rstrip(" ,;:") + "."
    return text


def clean_keywords(raw: list | str | None) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;]", raw)
    out: list[str] = []
    for item in raw or []:
        term = re.sub(r"\s+", " ", str(item or "")).strip().strip('"')
        if not term or term in out:
            continue
        out.append(term[:60])
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def _content_tokens(text: str) -> list[str]:
    return [
        tok.lower()
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9-']{2,}", text or "")
        if tok.lower() not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Labelers
# ---------------------------------------------------------------------------


def labeler_prompt(doc_class: str, text: str) -> str:
    labels = ", ".join(INTENT_LABELS.get(doc_class, ()))
    description = INTENT_DESCRIPTIONS.get(doc_class, "")
    return (
        "You are labeling legal documents for a mailroom evaluation set. "
        "Ground every label strictly in the document text below — no inferences "
        "beyond what the text states.\n\n"
        f"Document class: {doc_class}\n\n"
        "Document:\n"
        f"{text}\n\n"
        "Emit exactly three labels:\n"
        "1. intent — one controlled purpose label, chosen only from this closed "
        f"list: {labels}. {description}\n"
        "2. subject_matter — ONE tight sentence (<=60 words) summarizing what "
        "this document is about, quoted or paraphrased from the text.\n"
        "3. keywords — up to 8 short terms (1-4 words each) that appear in, or "
        "are directly grounded in, the text.\n\n"
        "Return ONLY a valid json object with keys intent, subject_matter, "
        "keywords. Do not include any text outside the json object. Output "
        "strict JSON only.\n\n"
        f"JSON schema:\n{json.dumps(LABEL_SCHEMA)}"
    )


def mock_label_row(row: dict, doc_class: str) -> dict:
    """Deterministic labeler for --mock / --dry-run (NEVER published as real)."""
    text = re.sub(r"\s+", " ", str(row.get("doc_text") or "")).strip()
    subclass = str(row.get("expected_subclass") or "")
    intent = normalize_intent(doc_class, subclass) or INTENT_LABELS[doc_class][0]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentence = sentences[0] if sentences else ""
    subject = clean_subject_matter(sentence) or f"{doc_class} document."
    tokens = _content_tokens(text)
    freq = [w for w, _ in Counter(tokens).most_common(MAX_KEYWORDS) if len(w) >= 3]
    return {"intent": intent, "subject_matter": subject, "keywords": freq}


class RealLabeler:
    """LLM labeler via the mailroom client (retry + schema boilerplate).

    ``model`` overrides the taxonomy `judge` agent's model — the labeler is a
    bulk offline job whose output is a CSV (no trace value), so the cheapest
    JSON-capable model (qwen/qwen3.7-flash) is the cost-effective default.
    """

    def __init__(self, model: str | None = None) -> None:
        self.client, default_model = get_llm("judge")
        self.model = model or default_model
        logger.info("labeler_llm", model=self.model)

    def label(self, row: dict, doc_class: str) -> dict:
        text = head_tail_window(str(row.get("doc_text") or ""))
        user_message = labeler_prompt(doc_class, text)
        raw = ""
        # Empty 200 completions are NOT retried by retry_chat_completion (that
        # wrapper handles HTTP-level transients only) — some providers return
        # an empty message on overload; retry once with backoff before failing.
        for attempt in range(2):
            resp = retry_chat_completion(
                self.client,
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You label legal documents with a controlled purpose "
                            "label, a one-sentence subject, and grounded keywords. "
                            "\n\nOutput must be a single json object conforming to "
                            "the provided json schema (response_format is json_object)."
                        ),
                    },
                    {"role": "user", "content": user_message.strip()},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=1024,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if raw:
                break
            if attempt == 0:
                logger.warning("labeler_empty_completion", model=self.model, retrying=True)
                time.sleep(3.0)
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"labeler non-JSON output: {raw[:200]}")
        intent = normalize_intent(doc_class, parsed.get("intent"))
        if not intent:
            raise ValueError(
                f"labeler intent {parsed.get('intent')!r} not in {doc_class} vocabulary: "
                f"{', '.join(INTENT_LABELS.get(doc_class, ()))}"
            )
        return {
            "intent": intent,
            "subject_matter": clean_subject_matter(parsed.get("subject_matter")),
            "keywords": clean_keywords(parsed.get("keywords")),
        }


# ---------------------------------------------------------------------------
# Hub fetch
# ---------------------------------------------------------------------------


def _viewer_rows(config: str, split: str, offset: int, length: int) -> list[dict]:
    import httpx

    params = {
        "dataset": FULL_CORPUS_ID,
        "config": config,
        "split": split,
        "offset": offset,
        "length": min(int(length), 100),
    }
    headers = {}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Exception | None = None
    for attempt in range(5):
        try:
            resp = httpx.get(f"{VIEWER_BASE}/rows", params=params, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json().get("rows") or []
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"HF viewer failed ({config}/{split} offset={offset}): {last}")


def fetch_config_rows(config: str, split: str = "train", max_scan: int = 0) -> list[dict]:
    """All rows of one dataset config via datasets, viewer-pagination fallback."""
    if max_scan and max_scan > 0:
        cap = int(max_scan)
    else:
        cap = None
    try:
        from datasets import load_dataset  # type: ignore

        kwargs: dict = {"split": split}
        if FULL_CORPUS_REVISION:
            kwargs["revision"] = FULL_CORPUS_REVISION
        ds = load_dataset(FULL_CORPUS_ID, config, **kwargs)
        rows = [dict(r) for r in ds]
    except Exception:
        rows = []
        offset = 0
        while True:
            batch = _viewer_rows(config, split, offset, 100)
            if not batch:
                break
            rows.extend(b.get("row") if isinstance(b, dict) and "row" in b else b for b in batch)
            offset += len(batch)
            if cap is not None and offset >= cap:
                break
            if len(batch) < 100:
                break
    if cap is not None:
        rows = rows[:cap]
    return rows


def load_rows(split: str, max_scan: int) -> tuple[list[dict], list[dict]]:
    """(default rows with doc_text, ground_truth rows) on the pinned revision."""
    gt_rows = fetch_config_rows("ground_truth", split, max_scan)
    text_by_filename = {}
    for row in fetch_config_rows("default", split, max_scan):
        name = str(row.get("filename") or "").strip()
        if name:
            text_by_filename[name] = row.get("doc_text") or row.get("text") or ""
    for row in gt_rows:
        row.setdefault("doc_text", text_by_filename.get(str(row.get("filename") or "").strip(), ""))
    return text_by_filename, gt_rows


# ---------------------------------------------------------------------------
# Assembly / push
# ---------------------------------------------------------------------------


def _write_sidecar(save_dir: Path, mode: str, labeled: int) -> Path:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / "label_run.json"
    payload = {
        "mode": mode,
        "labeled": labeled,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _load_provenance(save_dir: Path) -> dict:
    path = Path(save_dir) / "label_run.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_new_rows(
    gt_rows: list[dict],
    *,
    real: bool,
    limit: int,
    save_dir: Path,
    resume_labels: dict[str, dict] | None = None,
    checkpoint_every: int = 25,
    model: str | None = None,
) -> tuple[list[dict], dict]:
    """Add intent/subject_matter/keywords to the labeled classes' GT rows.

    Checkpoints the preview CSV every ``checkpoint_every`` rows so a
    long-running real label pass survives a kill — ``--resume`` reuses those
    labels keyed by filename. Returns (new rows in original order, stats).
    """
    save_dir = Path(save_dir)
    labeler = RealLabeler(model=model) if real else None
    out_rows: list[dict] = []
    stats: dict = {"labeled": 0, "skipped_short_text": 0, "skipped_unknown_class": 0, "resumed": 0}
    per_class: Counter = Counter()
    per_intent: Counter = Counter()
    kw_stats: Counter = Counter()
    seen = 0
    resumed_labels = dict(resume_labels or {})
    for idx, row in enumerate(gt_rows):
        cls = str(row.get("expected") or row.get("expected_doc_class") or "").strip()
        if cls not in LABELED_CLASSES:
            out_rows.append({k: v for k, v in row.items() if k != "doc_text"})
            continue
        if limit and seen >= limit:
            out_rows.append({k: v for k, v in row.items() if k != "doc_text"})
            continue
        filename = str(row.get("filename") or "").strip()
        existing = resumed_labels.get(filename)
        if existing:
            out_rows.append({
                **{k: v for k, v in row.items() if k != "doc_text"},
                "intent": existing["intent"],
                "subject_matter": existing["subject_matter"],
                "keywords": json.dumps(existing["keywords"], ensure_ascii=False),
            })
            seen += 1
            stats["resumed"] += 1
            per_class[cls] += 1
            per_intent[(cls, existing["intent"])] += 1
            kw_stats["keywords_n"] += len(existing["keywords"])
        else:
            text = str(row.get("doc_text") or "").strip()
            if len(text) < 200:
                stats["skipped_short_text"] += 1
                logger.warning("label_skip_short_text", filename=filename, cls=cls)
                out_rows.append({k: v for k, v in row.items() if k != "doc_text"})
                continue
            try:
                labels = labeler.label(row, cls) if labeler else mock_label_row(row, cls)
            except Exception as exc:
                logger.warning("label_failed", filename=filename, cls=cls, error=str(exc)[:200])
                out_rows.append({k: v for k, v in row.items() if k != "doc_text"})
                continue
            new_row = {k: v for k, v in row.items() if k != "doc_text"}
            new_row["intent"] = labels["intent"]
            new_row["subject_matter"] = labels["subject_matter"]
            new_row["keywords"] = json.dumps(labels["keywords"], ensure_ascii=False)
            out_rows.append(new_row)
            seen += 1
            stats["labeled"] += 1
            per_class[cls] += 1
            per_intent[(cls, labels["intent"])] += 1
            kw_stats["keywords_n"] += len(labels["keywords"])
        if (seen + stats["skipped_short_text"]) and (seen) and (seen + stats["skipped_short_text"]) % checkpoint_every == 0:
            _write_preview(save_dir, out_rows)
            _write_sidecar(save_dir, "real" if real else "mock", stats["labeled"])
            print(
                f"  checkpoint: {stats['labeled'] + stats['resumed']} labeled/"
                f"{len(out_rows)} rows @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}Z",
                flush=True,
            )
    _write_preview(save_dir, out_rows)
    _write_sidecar(save_dir, "real" if real else "mock", stats["labeled"])
    stats["per_class"] = dict(per_class)
    stats["per_intent"] = {f"{c}/{i}": n for (c, i), n in per_intent.items()}
    if kw_stats["keywords_n"]:
        stats["avg_keywords"] = round(kw_stats["keywords_n"] / (stats["labeled"] + stats["resumed"]), 2)
    return out_rows, stats


def _write_preview(save_dir: Path, rows: list[dict]) -> Path:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / "ground_truth_preview.csv"
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in keys})
    return path


def load_preview_labels(save_dir: Path) -> dict[str, dict]:
    """Existing label preview rows by filename — for ``--reuse`` (no LLM)."""
    path = Path(save_dir) / "ground_truth_preview.csv"
    if not path.exists():
        return {}
    labels: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = str(row.get("filename") or "").strip()
            if not name or not row.get("intent"):
                continue
            labels[name] = {
                "intent": row["intent"],
                "subject_matter": row["subject_matter"],
                "keywords": json.loads(row["keywords"]) if row.get("keywords") else [],
            }
    return labels


def reuse_preview_rows(
    gt_rows: list[dict],
    preview_labels: dict[str, dict],
) -> tuple[list[dict], dict]:
    """Merge preview labels onto fresh GT rows (no LLM calls)."""
    out_rows: list[dict] = []
    stats: dict = {"reused": 0, "missing": 0}
    for row in gt_rows:
        cls = str(row.get("expected") or row.get("expected_doc_class") or "").strip()
        clean = {k: v for k, v in row.items() if k != "doc_text"}
        if cls not in LABELED_CLASSES:
            out_rows.append(clean)
            continue
        labels = preview_labels.get(str(row.get("filename") or "").strip())
        if not labels:
            stats["missing"] += 1
            logger.warning(
                "reuse_label_missing", filename=row.get("filename"), cls=cls
            )
            out_rows.append(clean)
            continue
        out_rows.append({
            **clean,
            "intent": labels["intent"],
            "subject_matter": labels["subject_matter"],
            "keywords": json.dumps(labels["keywords"], ensure_ascii=False),
        })
        stats["reused"] += 1
    return out_rows, stats


def write_parquet(rows: list[dict], path: Path) -> Path:
    """Carve rows into a single parquet file (pyarrow; no `datasets` dep)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = Path(path)
    safe_rows = [{k: ("" if v is None else v) for k, v in row.items()} for row in rows]
    keys: list[str] = []
    for row in safe_rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    arrays = [pa.array([r.get(k) for r in safe_rows], pa.string()) for k in keys]
    table = pa.Table.from_arrays(arrays, names=keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def push_revision(rows: list[dict], *, commit_message: str) -> str:
    """Push a new ground_truth train split; returns the new target commit sha.

    Writes the enriched rows to ``parquet/ground_truth/train/`` and uploads the
    single parquet file via ``huggingface_hub`` (no ``datasets`` dependency) —
    the rest of the repo (default config, ground_truth test split, README) is
    preserved untouched.
    """
    import tempfile

    import huggingface_hub  # type: ignore

    api = huggingface_hub.HfApi()
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit("--push requires HF_TOKEN (or HUGGING_FACE_HUB_TOKEN) in .env")
    with tempfile.TemporaryDirectory() as tmp:
        local = write_parquet(rows, Path(tmp) / "train-00000-of-00001.parquet")
        api.upload_file(
            path_or_fileobj=str(local),
            path_in_repo="parquet/ground_truth/train/train-00000-of-00001.parquet",
            repo_id=FULL_CORPUS_ID,
            repo_type="dataset",
            commit_message=commit_message,
            token=token,
        )
    refs = api.list_repo_refs(FULL_CORPUS_ID, repo_type="dataset", token=token)
    branch = next((b for b in refs.branches if b.name == "main"), refs.branches[0])
    sha = str(branch.target_commit)
    print(f"pushed ground_truth revision: {FULL_CORPUS_ID} @ {sha}")
    return sha


def update_revision_pin(sha: str) -> bool:
    path = SRC_DIR / "pipeline" / "hf_corpora.py"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(
        r'FULL_CORPUS_REVISION = "[0-9a-f]{40}"',
        f'FULL_CORPUS_REVISION = "{sha}"',
        text,
        count=1,
    )
    if n != 1:
        logger.error("revision_pin_not_found", path=str(path))
        return False
    path.write_text(new, encoding="utf-8")
    print(f"updated FULL_CORPUS_REVISION -> {sha} in {path}")
    return True


def check_contract() -> int:
    """Network-free sanity checks for the derive/publish pipeline."""
    for cls in LABELED_CLASSES:
        assert cls in INTENT_LABELS, f"missing intent vocabulary: {cls}"
        assert len(INTENT_LABELS[cls]) >= 2
    assert normalize_intent("correspondence", "demand letter") == "payment_demand"
    assert normalize_intent("corporate_record", "bylaws") == "governance_rules"
    assert normalize_intent("insurance_claim", "claim denied") == "coverage_determination"
    assert normalize_intent("correspondence", "made-up-thing") == ""
    assert normalize_intent("contract", "anything") == ""
    subj = clean_subject_matter("  A very long sentence about stuff.   ")
    assert subj.endswith(".")
    assert len(subj) <= MAX_SUBJECT_CHARS
    assert clean_keywords(["a", "a", "b"]) == ["a", "b"]
    assert len(clean_keywords([f"k{i}" for i in range(20)])) == MAX_KEYWORDS
    win = head_tail_window("x" * (MAX_TEXT_CHARS * 2))
    assert "[truncated]" in win and len(win) < MAX_TEXT_CHARS * 2
    labels = mock_label_row(
        {"doc_text": "MEMORANDUM RE: payment of $50,000. The matter is urgent.", "expected_subclass": "letter"},
        "correspondence",
    )
    assert labels["intent"] in INTENT_LABELS["correspondence"]
    assert labels["subject_matter"]
    assert len(labels["keywords"]) <= MAX_KEYWORDS
    print("check_contract OK — labeler, vocabulary, cleanup, and mock path verified.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive + publish purpose/gist GT to mailroom-corpus.")
    parser.add_argument("--check", action="store_true", help="Network-free contract checks.")
    parser.add_argument("--real", action="store_true", help="LLM labeler from document text.")
    parser.add_argument("--mock", action="store_true", help="Deterministic labeler (machinery only).")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + derive, preview CSV, no push.")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Load labels from the existing preview CSV instead of invoking the LLM "
        "(push the reviewed labels without spending calls).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a killed real label pass from the checkpointed preview CSV "
        "(skips filenames that already carry labels).",
    )
    parser.add_argument("--push", action="store_true", help="Push a new ground_truth revision to Hub.")
    parser.add_argument("--no-update-pin", action="store_true", help="Do not rewrite FULL_CORPUS_REVISION.")
    parser.add_argument("--limit", type=int, default=0, help="Bound labeled rows per class (0 = all).")
    parser.add_argument(
        "--model",
        default="",
        help="Labeler LLM model id (default: the taxonomy `judge` agent's model). "
        "Cheapest JSON-capable spot for this bulk CSV job: qwen/qwen3.7-flash.",
    )
    parser.add_argument("--split", default="train")
    parser.add_argument("--max-scan", type=int, default=0, help="Bound total scanned rows (0 = all).")
    parser.add_argument("--commit", default="", help="Custom push commit message.")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "data" / "hf_gt",
        help="Preview output dir (default data/hf_gt).",
    )
    args = parser.parse_args()

    if args.check:
        return check_contract()

    if args.real and args.mock:
        raise SystemExit("--real and --mock are mutually exclusive.")
    if args.push and not (args.real or args.mock or args.reuse):
        raise SystemExit("--push requires --real, --mock, or --reuse.")

    if args.reuse and args.resume:
        raise SystemExit("--reuse and --resume are mutually exclusive.")

    if args.mock:
        print("WARNING: --mock labels are deterministic placeholders — never publish them to Hub.")

    text_by_filename, gt_rows = load_rows(args.split, args.max_scan)
    print(
        f"loaded {len(gt_rows)} ground_truth rows / {len(text_by_filename)} default rows "
        f"({FULL_CORPUS_ID} @ {FULL_CORPUS_REVISION or 'HEAD'})"
    )
    if not gt_rows:
        raise SystemExit("no ground_truth rows fetched — nothing to label.")

    if args.reuse:
        preview_labels = load_preview_labels(args.out)
        if not preview_labels:
            raise SystemExit(
                f"no reviewed labels found at {args.out / 'ground_truth_preview.csv'} "
                "— run --real (or --mock) first to produce a preview."
            )
        rows, stats = reuse_preview_rows(gt_rows, preview_labels)
        print(f"reused labels from preview: {stats['reused']} labeled / {stats['missing']} missing")
    else:
        resume_labels: dict[str, dict] = {}
        if args.resume:
            provenance = _load_provenance(args.out)
            if provenance.get("mode") != "real":
                raise SystemExit(
                    f"refusing to resume: {args.out / 'label_run.json'} says mode="
                    f"{provenance.get('mode')!r} — resuming requires a real-label checkpoint."
                )
            resume_labels = load_preview_labels(args.out)
            print(f"resuming from {args.out / 'ground_truth_preview.csv'}: "
                  f"{len(resume_labels)} already-labeled files")
        rows, stats = build_new_rows(
            gt_rows,
            real=args.real,
            limit=args.limit,
            save_dir=args.out,
            resume_labels=resume_labels or None,
            model=args.model or None,
        )
        print(
            f"labeled={stats['labeled']} resumed={stats['resumed']} "
            f"short-text-skip={stats['skipped_short_text']} "
            f"unknown-class-skip={stats['skipped_unknown_class']}"
        )
        for cls, n in sorted(stats.get("per_class", {}).items()):
            print(f"  {cls}: {n}")
        for key, n in sorted(stats.get("per_intent", {}).items()):
            print(f"    intent {key}: {n}")
        if stats.get("avg_keywords"):
            print(f"  avg keywords/doc: {stats['avg_keywords']}")
    print(f"preview CSV: {args.out / 'ground_truth_preview.csv'}")

    if args.push:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        commit = args.commit or (
            f"ground truth: intent + subject_matter + keywords for "
            f"{', '.join(LABELED_CLASSES)} ({stamp})"
        )
        sha = push_revision(rows, commit_message=commit)
        if not args.no_update_pin:
            if update_revision_pin(sha):
                print("next HF pilot will load the enriched labels; resting watchers required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())