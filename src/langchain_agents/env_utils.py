# VENDORED from github.com/Exios66/llm-entity-extraction (verified against commit 3a03d5c, 2026-08-10).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Environment variable helpers.

Require specific env vars are set, with helpful error messages, and load the
repo's dotenv files (``braintrust.env`` first, then ``.env``) so scripts can
run without exporting anything. Real shell environment variables always win.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load ``braintrust.env`` then ``.env`` into the environment (idempotent).

    Existing environment variables are never overridden. Safe to call from any
    script entry point.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for env_file in (REPO_ROOT / "braintrust.env", REPO_ROOT / ".env"):
        if env_file.exists():
            load_dotenv(env_file, override=False)


def require_env(*names: str) -> tuple[str, ...]:
    """Validate that all given environment variables are set and non-empty.

    Returns the resolved values as a tuple. Exits with a helpful message if any are missing.
    """
    load_env()
    values = []
    missing = []
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value:
            missing.append(name)
        else:
            values.append(value)
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    return tuple(values)


def get_env(name: str, default: str = "") -> str:
    """Get an environment variable with a default fallback."""
    load_env()
    return os.environ.get(name, default).strip()


def bool_env(name: str, default: bool = False) -> bool:
    """Get a boolean environment variable."""
    value = os.environ.get(name, str(default)).lower()
    return value in ("true", "1", "yes", "on")
