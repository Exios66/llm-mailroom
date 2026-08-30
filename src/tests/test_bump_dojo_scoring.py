"""Offline tests for src/scripts/bump_dojo_scoring.py rewrite helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.bump_dojo_scoring import (
    PIN_RE,
    _normalize_tag,
    _rewrite_text,
    apply_pin,
    current_pin,
)


def test_normalize_tag_adds_v_prefix():
    assert _normalize_tag("0.12.2") == "v0.12.2"
    assert _normalize_tag("v0.12.2") == "v0.12.2"


def test_normalize_tag_rejects_garbage():
    with pytest.raises(ValueError):
        _normalize_tag("latest")
    with pytest.raises(ValueError):
        _normalize_tag("")


def test_pin_re_matches_pyproject_line():
    line = '    "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.12.1",'
    match = PIN_RE.search(line)
    assert match is not None
    assert match.group(2) == "v0.12.1"


def test_rewrite_updates_pin_and_version_assert():
    before = (
        'llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.12.1\n'
        'assert llm_dojo_scoring.__version__ == "0.12.1"\n'
        "def test_installed_dojo_is_v0121():\n"
        "pass\n"
        "Pin: `llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.12.1`\n"
    )
    after = _rewrite_text(before, "v0.12.2")
    assert "@v0.12.2" in after
    assert '== "0.12.2"' in after
    assert "test_installed_dojo_is_v0122" in after
    assert "@v0.12.1" not in after


def test_apply_pin_dry_run_and_write(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = [\n'
        '    "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.12.1",\n'
        "]\n",
        encoding="utf-8",
    )
    assert current_pin(tmp_path) == "v0.12.1"
    changed = apply_pin("v0.12.2", root=tmp_path, dry_run=True)
    assert len(changed) == 1
    assert current_pin(tmp_path) == "v0.12.1"  # dry-run did not write
    apply_pin("v0.12.2", root=tmp_path, dry_run=False)
    assert current_pin(tmp_path) == "v0.12.2"
