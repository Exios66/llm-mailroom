"""LegalBench suite CLI.

Run a LegalBench task, trace it to Langfuse, and log the completed run to the
experiment log + experiment-log site:

    python -m legalbench.cli --task contract_qa --n 30 --model qwen/qwen3.7-flash
    python -m legalbench.cli --task family_classification --n 20 --mock --no-log
    python -m legalbench.cli --list-tasks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .runner import DEFAULT_MODEL, log_run, print_summary, run_task
from .tasks import task_help

TASK_CHOICES = ("contract_qa", "family_classification")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m legalbench.cli",
        description="Run a LegalBench evaluation task (local corpora, "
                    "Langfuse-traced, experiment-logged).",
    )
    parser.add_argument("--task", choices=TASK_CHOICES, default="contract_qa",
                        help="which LegalBench task to run")
    parser.add_argument("--n", type=int, default=30,
                        help="number of questions/documents to sample (default 30)")
    parser.add_argument("--seed", type=int, default=42,
                        help="deterministic sample seed (default 42)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"OpenRouter model id (default {DEFAULT_MODEL})")
    parser.add_argument("--mock", action="store_true",
                        help="deterministic fake model — no API key, no network, "
                             "NOT real results")
    parser.add_argument("--no-trace", action="store_true",
                        help="skip Langfuse tracing for this run")
    parser.add_argument("--no-log", action="store_true",
                        help="do not append to the experiment log or rebuild the site")
    parser.add_argument("--jsonl", type=Path, default=None,
                        help="experiment-log JSONL path override")
    parser.add_argument("--list-tasks", action="store_true",
                        help="list available tasks and exit")
    parser.add_argument("--version", action="version", version=f"legalbench {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_tasks:
        print("LegalBench suite tasks:")
        print(task_help())
        return 0

    result = run_task(
        args.task,
        n=args.n,
        seed=args.seed,
        model=args.model,
        mock=args.mock,
        trace_enabled=not args.no_trace,
    )
    print_summary(result)

    if not args.no_log:
        touched = log_run(result, jsonl_path=str(args.jsonl) if args.jsonl else None)
        for label, path in touched.items():
            if path:
                print(f"  {label}: {path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
