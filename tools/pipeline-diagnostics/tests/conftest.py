"""Hermetic fixtures for the nested diagnostics suite.

``src/tests/conftest.py`` does not apply here (this suite is outside the root
test root), so this file carries the whole isolation contract: no network, no
real model client, no writes outside ``tmp_path``. No test may reach a model or
a hosted service; the socket block makes an accidental attempt fail loudly.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "contract" / "mailroom-contract.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _no_network(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("network access is disabled in pipeline-diagnostics tests")


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path_factory.mktemp("mailroom-base")))
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)


@pytest.fixture
def cli_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Environment for subprocess CLI runs: offline, tracing off, no real key."""
    env = dict(os.environ)
    env.update(
        {
            "OBSERVABILITY_PROVIDER": "none",
            "OPENROUTER_API_KEY": "test-key-not-used",
            # Verbose logging on purpose: even then, stdout must stay pure JSON.
            "LOG_LEVEL": "DEBUG",
            "MAILROOM_BASE_DIR": str(tmp_path_factory.mktemp("mailroom-subprocess")),
        }
    )
    env.pop("PYTHONPATH", None)
    return env


@pytest.fixture(scope="session")
def contract_path() -> Path:
    return CONTRACT_PATH


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def minimal_contract() -> Path:
    return FIXTURES / "minimal-contract.json"


@pytest.fixture
def repo_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def mutated_contract(tmp_path: Path) -> Callable[[Callable[[dict[str, Any]], None]], Path]:
    """Write a mutated copy of the committed contract; the original is untouched."""

    def write(mutate: Callable[[dict[str, Any]], None]) -> Path:
        data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        mutate(data)
        path = tmp_path / "contract.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return write


@pytest.fixture
def run_cli(cli_env: dict[str, str]) -> Callable[..., Any]:
    """Run the CLI in a fresh interpreter, exactly as an agent would."""
    import subprocess

    def run(*args: str) -> Any:
        return subprocess.run(
            [sys.executable, "-m", "mailroom_diagnostics", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=cli_env,
            timeout=120,
            check=False,
        )

    return run
