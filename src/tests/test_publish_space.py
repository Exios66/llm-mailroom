"""Network-free pins for the Hugging Face producer Space publisher."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src" / "scripts" / "publish_space.py"


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish_space", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_payload_passes():
    mod = _load_publish()
    notes = mod.check_payload()
    joined = "\n".join(notes)
    assert "sdk=docker" in joined
    assert "7860" in joined
    assert "src/" in joined


def test_check_is_cli_zero():
    mod = _load_publish()
    assert mod.main(["--check"]) == 0


def test_dockerfile_binds_off_loopback_on_spaces_port():
    docker = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "MAILROOM_API_HOST=0.0.0.0" in docker
    assert "7860" in docker
    assert "python -m api.main" in docker or "api.main" in docker
    assert "MAILROOM_EMBED_WATCHER=1" in docker


def test_space_card_frontmatter_and_token_contract():
    card = (REPO_ROOT / "deploy" / "space" / "SPACE_README.md").read_text(
        encoding="utf-8"
    )
    assert card.lstrip().startswith("---")
    assert "sdk: docker" in card
    assert "app_port: 7860" in card
    assert "MAILROOM_PIPELINE_URL" in card
    assert "MAILROOM_PIPELINE_TOKEN" in card
    assert "MAILROOM_PIPELINE_API_PREFIX" in card
    assert "MAILROOM_API_TOKEN" in card
    assert "/v1/upload" in card
    pairing = REPO_ROOT / "deploy" / "space" / "PAIRING.md"
    text = pairing.read_text(encoding="utf-8")
    assert "MAILROOM_PIPELINE_API_PREFIX=/v1" in text
    assert "POST /v1/upload" in text
    assert "mailroom-observatory" in text
    assert "The-Mailroom/pull/30" in text
    assert "lucius-morningstar-mailroom-observatory.hf.space" in text
    assert "Lucius-Morningstar/mailroom-producer" in text
    assert "probe_hosted_spaces.py" in text


def test_stage_space_tree_strips_dotenv(tmp_path):
    mod = _load_publish()
    dest = tmp_path / "space"
    mod.stage_space_tree(dest)
    assert (dest / "README.md").is_file()
    assert (dest / "Dockerfile").is_file()
    assert (dest / "src" / "config" / "taxonomy.yaml").is_file()
    assert (dest / "src" / "api" / "main.py").is_file()
    assert not (dest / "src" / "tests").exists()
    assert list(dest.rglob(".env")) == []


def test_secret_values_require_producer_token(monkeypatch):
    mod = _load_publish()
    monkeypatch.delenv("MAILROOM_API_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        mod._secret_values(require_token=True)


def test_secret_values_map_pipeline_token(monkeypatch):
    mod = _load_publish()
    monkeypatch.setenv("MAILROOM_API_TOKEN", "shared-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    secrets = mod._secret_values(require_token=True)
    assert secrets["MAILROOM_API_TOKEN"] == "shared-secret"
    assert "OPENROUTER_API_KEY" not in secrets


def test_compose_producer_file_pairs_visualizer():
    compose = (
        REPO_ROOT / "deploy" / "docker-compose.producer.yml"
    ).read_text(encoding="utf-8")
    assert "MAILROOM_PIPELINE_URL=http://127.0.0.1:8000" in compose
    assert "MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN" in compose
    assert "MAILROOM_API_TOKEN" in compose
    assert "0.0.0.0" in compose
