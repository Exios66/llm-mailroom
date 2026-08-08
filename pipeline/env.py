"""Load environment variables from a .env file.

The app reads its configuration from environment variables (LLM keys, database
URL, observability keys, pipeline paths). A local `.env` file lets you keep those
in one place. Call `load_env()` early in any process entrypoint (watcher, API,
ops monitor) or standalone script.

`load_dotenv()` never overrides variables that are already set in the real
environment, so exported vars always win over the `.env` file.
"""

from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_env(path: str | Path | None = None) -> bool:
    global _loaded
    if _loaded:
        return True
    dotenv_path = path or (Path(__file__).resolve().parent.parent / ".env")
    _loaded = load_dotenv(dotenv_path=dotenv_path, override=False)
    return _loaded
