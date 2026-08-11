#!/usr/bin/env python3
"""Vision tradeoff sweep: run the same real documents under different vision
configurations and record accuracy vs cost/tokens, to find where page-image
ingestion is worth it and where it is not.

Configs compared (each a separate `run_pilot.py --real` process, isolated env):
  text-only   MAILROOM_VISION_ENABLED=0          (transcription only — no images)
  vision-10   MAILROOM_VISION_MAX_PAGES=10       (additive: full text + first 10 pages)
  vision-all  MAILROOM_VISION_MAX_PAGES=0        (additive: full text + ALL pages)

Every config processes the SAME documents (default: the 21 real committed
samples' first N — use --max-docs to bound the sweep cost). Because page-images
are now additive (full transcription always in the prompt), no configuration
ever drops document content; the question is purely how much the extra image
signal costs vs. what it buys in extraction/classification accuracy.

Usage:
    python scripts/run_vision_sweep.py --real --max-docs 3
    python scripts/run_vision_sweep.py --real --include contract --max-docs 3
    python scripts/run_vision_sweep.py --real --source atticus --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()
from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

CONFIGS = [
    ("text-only", {"MAILROOM_VISION_ENABLED": "0"}),          # transcribe-only
    ("vision-10", {"MAILROOM_VISION_MAX_PAGES": "10"}),       # additive + 10 pages
    ("vision-all", {"MAILROOM_VISION_MAX_PAGES": "0"}),       # additive + all pages
]

REPORT_PATH = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "pilot_report.json"
OUT_DIR = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "vision_sweep"


def _base_env() -> dict:
    env = {k: v for k, v in os.environ.items()}
    env.pop("MAILROOM_VISION_ENABLED", None)
    env.pop("MAILROOM_VISION_MAX_PAGES", None)
    env.pop("MAILROOM_VISION_DPI", None)
    return env


def run_config(name: str, overrides: dict, run_args: list[str], dry_run: bool) -> dict:
    env = _base_env()
    env.update(overrides)
    env["PYTHONPATH"] = str(REPO_ROOT)

    cmd = [sys.executable, "scripts/run_pilot.py", *run_args]
    logger.info("sweep_config_start", config=name, cmd=cmd)
    started = time.perf_counter()
    if dry_run:
        print(f"[dry-run] would run: {' '.join(cmd)} with env {overrides}")
        return {"config": name, "dry_run": True}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    elapsed = time.perf_counter() - started
    out_path = OUT_DIR / f"{name}.log.txt"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(proc.stdout + "\n---STDERR---\n" + proc.stderr)
    if proc.returncode != 0:
        logger.error("sweep_config_failed", config=name, rc=proc.returncode, log=str(out_path))
        return {
            "config": name,
            "rc": proc.returncode,
            "elapsed_s": round(elapsed, 2),
            "aborted": "cost cap" in proc.stderr or "abort" in proc.stderr.lower(),
        }

    report = json.loads(REPORT_PATH.read_text()) if REPORT_PATH.exists() else {}
    sweep_report = OUT_DIR / f"{name}.json"
    sweep_report.write_text(json.dumps(report, indent=2))
    logger.info("sweep_config_done", config=name, elapsed_s=round(elapsed, 2), report=str(sweep_report))
    return {
        "config": name,
        "rc": 0,
        "elapsed_s": round(elapsed, 2),
        "summary": report.get("summary", {}),
        "samples": report.get("samples", []),
        "scores": report.get("scores", {}).get("samples", []),
        "report_path": str(sweep_report),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision tradeoff sweep.")
    parser.add_argument("--real", action="store_true", help="Pass --real to run_pilot.")
    parser.add_argument("--mock", action="store_true", help="Pass --mock to run_pilot.")
    parser.add_argument("--include", help="Only samples of this doc class (pass-through).")
    parser.add_argument("--source", help="Only source corpus (pass-through).")
    parser.add_argument("--max-docs", type=int, default=3, help="Bound the sweep to N docs per config.")
    parser.add_argument("--configs", default=",".join(c[0] for c in CONFIGS), help="Comma-separated configs to run.")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands without running them.")
    args = parser.parse_args()

    if args.mock == args.real:
        parser.error("choose exactly one of --mock or --real")

    selected = [c for c in CONFIGS if c[0] in [x.strip() for x in args.configs.split(",")]]
    run_args = ["--mock" if args.mock else "--real", "--max-docs", str(args.max_docs)]
    if args.include:
        run_args += ["--include", args.include]
    if args.source:
        run_args += ["--source", args.source]

    results = [run_config(name, overrides, run_args, args.dry_run) for name, overrides in selected]
    if args.dry_run:
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUT_DIR / "sweep.json"
    summary_path.write_text(json.dumps(results, indent=2))

    print("\n== Sweep summary (see data/vision_sweep/) ==")
    for r in results:
        s = r.get("summary", {})
        if r.get("rc") != 0:
            print(f"  {r['config']:<12} FAILED rc={r.get('rc')} {r.get('aborted', '')}")
            continue
        print(
            f"  {r['config']:<12} samples={s.get('samples')} "
            f"class_acc={s.get('class_accuracy')} archived={s.get('archived')} review={s.get('review')} "
            f"cost=${s.get('total_cost_usd')} tokens={s.get('avg_tokens')} "
            f"field_score={s.get('avg_extraction_overall_score')} time_avg={s.get('avg_time_s')}s"
        )
    print(f"\nFull per-config reports written under {OUT_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
