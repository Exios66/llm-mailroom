"""Live ``mailroom-dataset`` manifest: resolve, verify, cache and emit CSV.

The Hub publishes a release ``manifest.txt`` with a sha256 for every file, not
a CSV. This module builds the pipeline's sample-manifest shape from the
ground-truth and blind parquet configs at an exact revision, verifies every
parquet file and every ``content_sha256``, and records the provenance needed
for a smoke run to be reproducible later.

The manifest never contains document text. Smoke runners resolve selected
documents by ``filename`` and verify the recorded ``content_sha256``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

HUB = "https://huggingface.co"
DATASET_ID = "Lucius-Morningstar/mailroom-dataset"
GT_CONFIG = "ground_truth"
BLIND_CONFIG = "default"
JOIN_KEY = "filename"
RELEASE_MANIFEST = "manifest.txt"
MANIFEST_SCHEMA_VERSION = "1.0.0"
DEFAULT_MAX_CHARS = 250_000
CLASSES = (
    "contract",
    "merger_agreement",
    "correspondence",
    "insurance_claim",
    "corporate_record",
)
MANIFEST_COLUMNS = (
    "id",
    "filename",
    "source",
    "dataset",
    "split",
    "hub_revision",
    "hub_sha",
    "expected_doc_class",
    "expected_subclass",
    "expected_stage",
    "expected_specialist",
    "sorter_subclass",
    "expected_fields",
    "content_sha256",
    "document_id",
    "review_expected",
    "retry_expected",
    "expected_post_retry_state",
    "matter_id",
    "group_id",
    "chars",
    "notes",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "llm-mailroom-live-manifest/1.0"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=_headers())
        response.raise_for_status()
        return response.content


def _get_json(url: str, *, timeout: float = 60.0) -> Any:
    return json.loads(_get_bytes(url, timeout=timeout))


def _cache_root() -> Path:
    from .hf_corpus_loader import cache_dir

    return cache_dir() / "live_manifest"


def _cached(url: str, label: str) -> bytes:
    target = _cache_root() / label
    if target.is_file() and target.stat().st_size > 0:
        return target.read_bytes()
    raw = _get_bytes(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _semver_key(name: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(name)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def resolve_revision(revision: str | None = None) -> dict[str, Any]:
    """Resolve a tag/branch/SHA; default is the newest semver tag.

    Returns ``{requested, tag, sha, main_sha}``. ``sha`` is the commit used for
    every resolve URL; ``main_sha`` records whether main has moved past it.
    """
    refs = _get_json(f"{HUB}/api/datasets/{DATASET_ID}/refs")
    tags = {
        str(item["name"]): str(item["targetCommit"])
        for item in refs.get("tags") or []
        if item.get("name") and item.get("targetCommit")
    }
    branches = {
        str(item["name"]): str(item["targetCommit"])
        for item in refs.get("branches") or []
        if item.get("name") and item.get("targetCommit")
    }
    main_sha = branches.get("main")
    requested = (revision or "").strip() or None
    if requested is None:
        candidates = sorted(
            ((name, sha) for name, sha in tags.items() if _semver_key(name)),
            key=lambda item: _semver_key(item[0]) or (0, 0, 0),
            reverse=True,
        )
        if candidates:
            tag, sha = candidates[0]
            return {"requested": None, "tag": tag, "sha": sha, "main_sha": main_sha}
        if main_sha:
            return {"requested": None, "tag": None, "sha": main_sha, "main_sha": main_sha}
        raise RuntimeError("mailroom-dataset has no resolvable tag or main commit")
    if requested in tags:
        return {"requested": requested, "tag": requested, "sha": tags[requested], "main_sha": main_sha}
    if requested in branches:
        return {"requested": requested, "tag": None, "sha": branches[requested], "main_sha": main_sha}
    if _COMMIT.match(requested):
        tag = next((name for name, sha in tags.items() if sha == requested), None)
        return {"requested": requested, "tag": tag, "sha": requested, "main_sha": main_sha}
    raise ValueError(f"unknown revision {requested!r} (tags: {sorted(tags)})")


def parse_release_manifest(text: str) -> dict[str, str]:
    """Parse ``manifest.txt``'s indented ``path sha256`` pairs."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\S+)\s+([0-9a-f]{64})\s*$", line)
        if match:
            checksums[match.group(1)] = match.group(2)
    if not checksums:
        raise RuntimeError("release manifest contained no path/sha256 pairs")
    return checksums


def _tree(sha: str) -> list[dict[str, Any]]:
    return _get_json(f"{HUB}/api/datasets/{DATASET_ID}/tree/{sha}?recursive=true")


def _parquet_paths(tree: Iterable[dict[str, Any]], config: str, split: str) -> list[str]:
    prefix = f"parquet/{config}/{split}/"
    return sorted(
        str(item["path"])
        for item in tree
        if str(item.get("path", "")).startswith(prefix)
        and str(item.get("path", "")).endswith(".parquet")
    )


def _download_verified(path: str, sha: str, checksums: dict[str, str]) -> bytes:
    url = f"{HUB}/datasets/{DATASET_ID}/resolve/{sha}/{path}"
    raw = _cached(url, f"{sha}/{path}")
    expected = checksums.get(path)
    if not expected:
        raise RuntimeError(f"release manifest has no checksum for {path}")
    if _sha256(raw) != expected:
        raise RuntimeError(f"checksum mismatch for {path}: expected {expected}, got {_sha256(raw)}")
    return raw


def load_frames(
    revision: str | dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Download and verify both configs at one exact revision.

    Pass a resolved ``{tag,sha,...}`` mapping when the caller already resolved
    it; passing a tag again could let a moved tag download a different commit
    than the one recorded in the manifest.
    """
    resolved = dict(revision) if isinstance(revision, dict) else resolve_revision(revision)
    sha = resolved["sha"]
    release = parse_release_manifest(
        _get_bytes(f"{HUB}/datasets/{DATASET_ID}/resolve/{sha}/{RELEASE_MANIFEST}").decode("utf-8")
    )
    tree = _tree(sha)
    frames: dict[str, pd.DataFrame] = {}
    downloads: dict[str, Any] = {}
    for config in (GT_CONFIG, BLIND_CONFIG):
        parts = []
        for path in _parquet_paths(tree, config, "train"):
            raw = _download_verified(path, sha, release)
            downloads[path] = {"sha256": _sha256(raw), "bytes": len(raw)}
            parts.append(pd.read_parquet(io.BytesIO(raw)))
        if not parts:
            raise RuntimeError(f"no parquet files for {config}/train at {sha}")
        frames[config] = pd.concat(parts, ignore_index=True)
    provenance = {
        "dataset": DATASET_ID,
        "revision": resolved,
        "release_manifest_verified": True,
        "downloads": downloads,
        "fetched_at": _now(),
    }
    return frames[GT_CONFIG], frames[BLIND_CONFIG], provenance


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _sorter_subclass(doc_class: str, subclass: str) -> str:
    """Normalize the Hub subclass to the sorter's closed token, when valid."""
    token = ""
    try:
        from langchain_agents.doc_inventories import (
            normalize_claim_type,
            normalize_communication_type,
            normalize_record_type,
            valid_sorter_subclasses,
        )
        from langchain_agents.cuad_maud import normalize_consideration
        from langchain_agents.sorter_agent import normalize_subtype

        if doc_class == "contract":
            token = normalize_subtype(subclass)
        elif doc_class == "merger_agreement":
            token = normalize_consideration(subclass)
        elif doc_class == "correspondence":
            token = normalize_communication_type(subclass)
        elif doc_class == "insurance_claim":
            token = normalize_claim_type(subclass)
        elif doc_class == "corporate_record":
            token = normalize_record_type(subclass)
        allowed = valid_sorter_subclasses(doc_class)
        if not token and subclass in allowed:
            token = subclass
        return token if token in allowed else ""
    except Exception:
        return ""


def _expected_fields(sample: dict[str, Any]) -> dict[str, Any]:
    from scripts.run_hf_pilot import expected_fields_for_sample

    fields = expected_fields_for_sample(sample)
    from schemas.documents import get_extraction_schema

    schema = get_extraction_schema(str(sample.get("expected_hf_class") or ""))
    if schema is not None:
        fields = {key: value for key, value in fields.items() if key in schema.model_fields}
    return {
        key: value
        for key, value in fields.items()
        if value not in (None, "", [], {})
    }


def build_manifest_rows(
    gt: pd.DataFrame, blind: pd.DataFrame, revision: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join labels to blind text and emit pipeline-manifest rows."""
    from scripts.run_hf_pilot import parse_hf_row

    merged = gt.merge(blind[[JOIN_KEY, "doc_text"]], on=JOIN_KEY, how="left", validate="one_to_one")
    rows: list[dict[str, Any]] = []
    integrity = {"checked": 0, "verified": 0, "mismatched": 0, "missing_text": 0}
    for _, record in merged.iterrows():
        raw = {key: _clean_value(value) for key, value in record.to_dict().items()}
        text = _coerce_text(raw.get("doc_text"))
        if not text.strip():
            integrity["missing_text"] += 1
            continue
        gt_fields_raw = raw.get("gt_fields")
        if isinstance(gt_fields_raw, str) and gt_fields_raw.strip():
            try:
                gt_fields = json.loads(gt_fields_raw)
            except json.JSONDecodeError:
                gt_fields = {}
            if isinstance(gt_fields, dict):
                raw = {**gt_fields, **raw}
        raw["doc_text"] = text
        sample = parse_hf_row(raw)
        if not sample:
            continue
        doc_class = str(sample.get("expected_hf_class") or "")
        if doc_class not in CLASSES:
            continue
        expected_sha = _coerce_text(raw.get("content_sha256"))
        actual_sha = _sha256(text.encode("utf-8"))
        integrity["checked"] += 1
        if expected_sha and expected_sha == actual_sha:
            integrity["verified"] += 1
        else:
            integrity["mismatched"] += 1
        fields = _expected_fields(sample)
        row_id = _coerce_text(raw.get("document_id")) or Path(_coerce_text(raw.get("filename"))).stem
        rows.append(
            {
                "id": row_id,
                "filename": _coerce_text(raw.get("filename")),
                "source": "hf:mailroom-dataset",
                "dataset": "mailroom-dataset",
                "split": _coerce_text(raw.get("split")) or "train",
                "hub_revision": revision.get("tag") or revision.get("sha") or "",
                "hub_sha": revision.get("sha") or "",
                "expected_doc_class": doc_class,
                "expected_subclass": str(sample.get("expected_subclass") or ""),
                "expected_stage": _coerce_text(raw.get("expected_stage")) or "archived",
                "expected_specialist": _coerce_text(raw.get("expected_specialist")),
                "sorter_subclass": _sorter_subclass(
                    doc_class, str(sample.get("expected_subclass") or "")
                ),
                "expected_fields": json.dumps(fields, sort_keys=True, separators=(",", ":")),
                "content_sha256": expected_sha or actual_sha,
                "document_id": _coerce_text(raw.get("document_id")),
                "review_expected": _coerce_text(raw.get("review_expected")).lower(),
                "retry_expected": _coerce_text(raw.get("retry_expected")).lower(),
                "expected_post_retry_state": _coerce_text(raw.get("expected_post_retry_state")),
                "matter_id": _coerce_text(raw.get("matter_id")),
                "group_id": _coerce_text(raw.get("group_id")),
                "chars": len(text),
                "notes": _coerce_text(raw.get("source_corpus")),
            }
        )
    # Stable unique ids even when the Hub repeats a document id or a suffixed
    # id collides with another generated suffix (A, A, A-2 must stay distinct).
    used: set[str] = set()
    for row in rows:
        base = row["id"] or Path(row["filename"]).stem
        candidate = base
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{base}-{suffix}"
        used.add(candidate)
        row["id"] = candidate
    rows.sort(key=lambda row: (row["expected_doc_class"], row["chars"], row["id"]))
    classes: dict[str, int] = {}
    for row in rows:
        classes[row["expected_doc_class"]] = classes.get(row["expected_doc_class"], 0) + 1
    stats = {
        "rows": len(rows),
        "classes": classes,
        "integrity": integrity,
    }
    return rows, stats


def write_manifest_csv(rows: list[dict[str, Any]], out: Path, provenance: dict[str, Any]) -> dict[str, Any]:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MANIFEST_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in MANIFEST_COLUMNS})
    os.replace(temporary, out)
    payload = {
        **provenance,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "csv": out.name,
        "csv_sha256": _sha256(out.read_bytes()),
        "rows": len(rows),
        "written_at": _now(),
    }
    sidecar = out.with_suffix(".provenance.json")
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """Read and validate a manifest CSV; raises ValueError on drift."""
    from pipeline.config import load_config

    path = Path(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(MANIFEST_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"manifest missing columns: {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("manifest is empty")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("manifest has duplicate ids")
    valid_classes = {c["key"] for c in load_config()["doc_classes"]}
    for row in rows:
        if row["expected_doc_class"] not in valid_classes:
            raise ValueError(f"{row['id']}: unknown class {row['expected_doc_class']!r}")
        if row["expected_stage"] not in ("archived", "review", "failed"):
            raise ValueError(f"{row['id']}: invalid expected_stage {row['expected_stage']!r}")
        if row["content_sha256"] and not _SHA256.match(row["content_sha256"]):
            raise ValueError(f"{row['id']}: invalid content_sha256")
        try:
            fields = json.loads(row["expected_fields"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{row['id']}: expected_fields is not JSON") from exc
        if not isinstance(fields, dict):
            raise ValueError(f"{row['id']}: expected_fields must be an object")
    return rows


def select_smoke_rows(
    rows: list[dict[str, Any]],
    *,
    per_class: int = 1,
    classes: Iterable[str] = CLASSES,
    max_chars: int = DEFAULT_MAX_CHARS,
    require_archived: bool = True,
) -> list[dict[str, Any]]:
    """Pick small, deterministic real documents, one or more per class."""
    selected: list[dict[str, Any]] = []
    for doc_class in classes:
        pool = [
            row
            for row in rows
            if row["expected_doc_class"] == doc_class
            and 200 <= int(row["chars"] or 0) <= max_chars
            and (not require_archived or row["expected_stage"] == "archived")
            and str(row.get("review_expected") or "").lower() not in ("true", "1")
        ]
        # No relaxed fallback: an oversized/non-archived document must be
        # explicitly opted into, never silently selected by the default smoke.
        pool.sort(key=lambda row: (int(row["chars"] or 0), row["id"]))
        selected.extend(pool[: max(0, per_class)])
    return selected


def fetch_texts(rows: list[dict[str, Any]], revision: dict[str, Any]) -> dict[str, str]:
    """Fetch blind text for selected rows and verify recorded content hashes."""
    sha = revision["sha"]
    tree = _tree(sha)
    release = parse_release_manifest(
        _get_bytes(f"{HUB}/datasets/{DATASET_ID}/resolve/{sha}/{RELEASE_MANIFEST}").decode("utf-8")
    )
    wanted = {row["filename"]: row for row in rows}
    texts: dict[str, str] = {}
    for split in sorted({row["split"] or "train" for row in rows}):
        for path in _parquet_paths(tree, BLIND_CONFIG, split):
            frame = pd.read_parquet(io.BytesIO(_download_verified(path, sha, release)))
            for filename, text in zip(frame[JOIN_KEY], frame["doc_text"]):
                name = str(filename)
                if name not in wanted or name in texts:
                    continue
                value = str(text or "")
                expected = wanted[name]["content_sha256"]
                if _sha256(value.encode("utf-8")) != expected:
                    raise RuntimeError(f"content_sha256 mismatch for {name}")
                texts[name] = value
    missing = sorted(set(wanted) - set(texts))
    if missing:
        raise RuntimeError(f"selected documents missing from blind config: {missing[:5]}")
    return texts
