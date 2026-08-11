"""Experiment-log integration for LegalBench runs.

On run completion the runner appends one JSON record (the sibling
``llm-entity-extraction`` experiment-log schema — the same append-only JSONL
the dedicated experiment-log site is built from) and regenerates:

1. the human-readable markdown log (via the upstream repo's
   ``scripts/reporting/render_experiment_log.py`` when that repo is present),
2. the experiment-log site data (via the upstream ``scripts/site/build_site.py``),
3. the synced copy in this repo at ``docs/reports/experiments/experiment_log.md``.

Everything is local file work — committing/pushing the sibling repo is a
separate, explicit step (``LEGALBENCH_SIBLING_REPO`` overrides the path).

Path resolution order (each knob is an env var):
- JSONL log:     ``LEGALBENCH_EXPERIMENT_LOG`` > sibling ``reports/experiment_log.jsonl`` > local ``legalbench/reports/experiment_log.jsonl``
- Sibling repo:  ``LEGALBENCH_SIBLING_REPO`` > ``<this repo>/../llm-entity-extraction``
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_LOG = REPO_ROOT / "legalbench" / "reports" / "experiment_log.jsonl"
SYNCED_MD = REPO_ROOT / "docs" / "reports" / "experiments" / "experiment_log.md"

SIBLING_ENV = "LEGALBENCH_SIBLING_REPO"
LOG_ENV = "LEGALBENCH_EXPERIMENT_LOG"


def default_sibling_root() -> Optional[Path]:
    env = os.environ.get(SIBLING_ENV)
    if env:
        path = Path(env).expanduser()
        if path.is_dir():
            return path
        return None
    candidate = REPO_ROOT.parent / "llm-entity-extraction"
    return candidate if candidate.is_dir() else None


def default_log_path() -> Path:
    env = os.environ.get(LOG_ENV)
    if env:
        return Path(env).expanduser()
    sibling = default_sibling_root()
    if sibling is not None:
        return sibling / "reports" / "experiment_log.jsonl"
    return LOCAL_LOG


def git_snapshot() -> dict[str, Any]:
    """llm-mailroom commit at run time (best-effort)."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=10, cwd=REPO_ROOT,
            ).stdout.strip()
        )
        return {"commit": commit or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return {"commit": None, "dirty": None}


def build_record(
    *,
    task_id: str,
    kind: str,
    experiment_name: str,
    prompt_version: str,
    model: str,
    rows: list[dict[str, Any]],
    n_requested: int,
    seed: int,
    scores: dict[str, Any],
    results: list[dict[str, Any]],
    tokens: dict[str, Any],
    parameters: Optional[dict[str, Any]] = None,
    data_source: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """One experiment-log record in the upstream schema (see the JSONL
    header of ``reports/experiment_log.jsonl`` in llm-entity-extraction)."""
    from . import data as _data

    fp = _data._fingerprint(rows)
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    n_error = len(results) - n_ok
    record = {
        "type": "experiment",
        "task": kind,  # site dispatch key: legalbench_binary_answer / _multiclass_classification
        "experiment_name": experiment_name,
        "git": git_snapshot(),
        "model": model,
        "prompt_version": prompt_version,
        "data_source": {
            "project": (data_source or {}).get("project", "local:cuad"),
            "ground_truth": (data_source or {}).get("ground_truth", ""),
            "dataset_fingerprint": fp,
            "n_samples": len(rows),
            "sample_requested": n_requested,
            "seed": seed,
        },
        "parameters": {
            "temperature": 0.1,
            "max_tokens": 4096,
            "max_input_chars": 100_000,
            "reasoning_effort": "none",
            "max_concurrency": 1,
            **(parameters or {}),
        },
        "tokens": tokens,
        "scores": scores,
        "n_rows": len(rows),
        "n_ok": n_ok,
        "n_error": n_error,
        "results": results,
        "suite": "legalbench",
        "task_id": task_id,
    }
    return record


def append_record(record: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Append one JSON line (stamped with an ISO timestamp if absent)."""
    path = Path(path or default_log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("timestamp", _dt.datetime.now(_dt.timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return path


# ------------------------------------------------------------- regeneration


def _run_python(script: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=600, cwd=cwd,
    )


def _inside(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def regenerate(log_path: Optional[Path] = None) -> dict[str, Any]:
    """Regenerate the markdown log + site data + synced docs copy.

    Returns {"log_md": path|None, "site_out": path|None, "synced_md": path}
    describing what was written. When the sibling repo is absent, or the log
    path is NOT inside the sibling repo (a custom/throwaway log), a minimal
    local markdown is written under ``legalbench/reports/`` — rebuilding the
    sibling's markdown/site from a partial log would clobber the real one.
    """
    log_path = Path(log_path or default_log_path())
    sibling = default_sibling_root()
    touched: dict[str, Any] = {"log_md": None, "site_out": None, "synced_md": None}

    can_rebuild_sibling = (
        sibling is not None
        and log_path.exists()
        and _inside(sibling, log_path)
    )

    if can_rebuild_sibling:
        render = sibling / "scripts" / "reporting" / "render_experiment_log.py"
        build_site = sibling / "scripts" / "site" / "build_site.py"
        sibling_md = sibling / "reports" / "experiment_log.md"
        site_out = sibling / "docs" / "data"
        try:
            if render.exists():
                proc = _run_python(
                    render,
                    ["--jsonl", str(log_path), "--markdown", str(sibling_md)],
                    cwd=sibling,
                )
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"render_experiment_log.py failed: {proc.stderr.strip()[:500]}"
                    )
                touched["log_md"] = str(sibling_md)
            if build_site.exists():
                proc = _run_python(
                    build_site,
                    ["--jsonl", str(log_path), "--out", str(site_out)],
                    cwd=sibling,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"build_site.py failed: {proc.stderr.strip()[:500]}")
                touched["site_out"] = str(site_out)
            if sibling_md.exists():
                touched["synced_md"] = str(_sync_markdown(sibling, sibling_md))
        except Exception as exc:  # regeneration is best-effort; the append stands
            print(f"  WARN: experiment-log regeneration failed: {exc}", file=sys.stderr)
    local_md = REPO_ROOT / "legalbench" / "reports" / "experiment_log.md"
    _write_local_markdown(log_path, local_md)
    if touched["log_md"] is None:
        touched["log_md"] = str(local_md)
    return touched


def _sync_markdown(sibling: Path, sibling_md: Path) -> Path:
    """Refresh the SYNCED copy in this repo's docs (upstream header format)."""
    try:
        commit = subprocess.run(
            ["git", "log", "-1", "--format=%h %s (%ad)", "--date=short"],
            capture_output=True, text=True, timeout=10, cwd=sibling,
        ).stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        commit = "?"
    today = _dt.date.today().isoformat()
    header = (
        "<!-- SYNCED DOCUMENT — do not hand-edit. Regenerate from the upstream repo. -->\n\n"
        "# Experiment Log\n\n"
        "> **Source (upstream):** Experiment log of **llm-entity-extraction** — the "
        "prompt-experiment loop environment for the llm-mailroom legal document pipeline.\n>\n"
        f"> - **Upstream repo:** https://github.com/Exios66/llm-entity-extraction\n"
        f"> - **Upstream path:** `reports/experiment_log.md`\n"
        f"> - **Upstream commit:** `{commit}`\n"
        f"> - **Synced into llm-mailroom:** {today}, verbatim, into `docs/reports/experiments/`\n"
        "> - **How it is produced upstream:** derived (rendered) from the append-only "
        "`reports/experiment_log.jsonl` via `python scripts/reporting/render_experiment_log.py` "
        "in that repo — **never hand-edited**.\n"
        "> - **Interactive viewer:** the same log is browsable as a clean static site "
        "(filterable runs index + per-run detail pages) at "
        "https://exios66.github.io/llm-entity-extraction/ — the associated GitHub Pages "
        "website for the upstream repo, served from its `docs/` folder (no Actions runners), "
        "rebuilt with `python scripts/site/build_site.py`.\n"
        "> - **How it relates to llm-mailroom:** the sorter and contracts-specialist agents "
        "vendored into `llm-mailroom/langchain_agents/` are the eval-validated prompts "
        "tracked here (`sorter_v5`, `contracts_specialist_v11`). The **LegalBench suite** "
        "(`llm-mailroom/legalbench/`) appends its runs to this same log and rebuilds it on "
        "completion.\n>\n"
        "> ---\n\n"
    )
    body = sibling_md.read_text(encoding="utf-8")
    # The upstream markdown starts with its own "# Experiment Log" heading —
    # strip it so the wrapper's heading is the single one.
    if body.startswith("# Experiment Log"):
        body = body.split("\n", 1)[1].lstrip("\n")
    SYNCED_MD.parent.mkdir(parents=True, exist_ok=True)
    SYNCED_MD.write_text(header + body, encoding="utf-8")
    return SYNCED_MD


def _write_local_markdown(log_path: Path, md_path: Path) -> None:
    """Minimal local markdown fallback when the sibling repo is absent."""
    records = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    lines = [
        "# LegalBench Experiment Log",
        "",
        f"_Generated from `{log_path.name}` — append-only, one section per run._",
        "",
    ]
    for i, record in enumerate(records, start=1):
        lines.append(f"## {i}. {record.get('experiment_name', 'run')}  ({record.get('task', '')})")
        for key in ("timestamp", "model", "prompt_version", "n_rows", "n_ok", "n_error"):
            if record.get(key) is not None:
                lines.append(f"- **{key}:** {record[key]}")
        scores = record.get("scores") or {}
        if scores:
            lines.append("")
            lines.append("### Scores")
            for k, v in scores.items():
                if not isinstance(v, dict):
                    lines.append(f"- {k}: {v}")
        lines.append("")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
