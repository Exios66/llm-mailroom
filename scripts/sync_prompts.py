#!/usr/bin/env python3
"""Push the local agent prompts into Langfuse Prompt Management.

One text prompt per agent, named `mailroom-<agent_name>` (see
`llm/prompts.py`), labeled `production`. Creating a prompt with an existing
name adds a new version and moves the `production` label to it, so repeated
syncs are only performed when the local template actually changed — the prompt
list stays clean and version history stays meaningful.

Usage:
    python scripts/sync_prompts.py              # sync all prompts
    python scripts/sync_prompts.py --dry-run    # show what would change
    python scripts/sync_prompts.py --force      # always create a new version
    python scripts/sync_prompts.py --agent sorter
"""

from __future__ import annotations

import argparse
import os
import sys

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing or unreachable) — nothing to sync.")
        return None
    return client


def _current_production(client, name: str) -> str | None:
    try:
        prompt = client.get_prompt(name, label="production")
        return prompt.prompt
    except Exception:
        return None


def sync_one(client, agent_name: str, template: str, *, force: bool, dry_run: bool) -> str:
    name = f"mailroom-{agent_name}"
    current = None if force else _current_production(client, name)
    if current == template:
        return f"unchanged  {name}"
    action = "force" if force else ("create" if current is None else "update")
    if dry_run:
        return f"{action:9s} {name}"
    client.create_prompt(name=name, type="text", prompt=template, labels=["production"])
    return f"{action:9s} {name}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync agent prompts to Langfuse prompt management.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without creating anything.")
    parser.add_argument("--force", action="store_true", help="Always create a new prompt version.")
    parser.add_argument("--agent", help="Only sync one agent (e.g. sorter).")
    args = parser.parse_args()

    from llm.prompts import prompt_templates

    templates = prompt_templates()
    if args.agent:
        if args.agent not in templates:
            print(f"Unknown agent '{args.agent}'. Available: {', '.join(sorted(templates))}")
            return 1
        templates = {args.agent: templates[args.agent]}

    client = _client()
    if client is None:
        return 1

    print(f"{'status':<10} prompt")
    print("-" * 60)
    changed = 0
    for agent_name, template in sorted(templates.items()):
        status = sync_one(client, agent_name, template, force=args.force, dry_run=args.dry_run)
        print(f"{status}")
        if not status.startswith("unchanged"):
            changed += 1

    if not args.dry_run:
        client.flush()
    print(f"\n{len(templates)} prompts checked, {changed} {'would change' if args.dry_run else 'synced'}.")
    if not args.dry_run:
        print("Prompts live at https://langfuse.com/docs/prompt-management (name prefix: mailroom-).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
