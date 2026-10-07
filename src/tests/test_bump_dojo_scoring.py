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


@pytest.mark.parametrize("tag", ["v0.20.0", "0.20.0"])
@pytest.mark.parametrize(
    "template",
    [
        pytest.param(
            'assert "llm-dojo-scoring.git@{version}" in pin\n',
            id="test-pin-assertion",
        ),
        pytest.param(
            "| Latest | v0.19.1 as of 2026-10-07 (llm-mailroom pins {version}; "
            "entity-extraction and agent-mailroom pin v0.16.0) |\n",
            id="consumer-pin-with-sibling-versions",
        ),
        pytest.param(
            "| llm-mailroom | llm-dojo-scoring | git pin `@{version}`, "
            "auto-bumped (as of 2026-10-07) |\n",
            id="dated-dependency-table",
        ),
        pytest.param(
            "![Dojo](https://img.shields.io/badge/dojo-{version}-6f42c1)\n",
            id="markdown-badge",
        ),
        pytest.param(
            '<img src="https://img.shields.io/badge/dojo-{version}-6f42c1" '
            'alt="llm-dojo-scoring {version}">\n',
            id="html-badge-and-alt",
        ),
    ],
)
def test_rewrite_gitbook_and_badge_pin_formats(template, tag):
    before = template.format(version="v0.18.0")
    expected = template.format(version="v0.20.0")

    assert _rewrite_text(before, tag) == expected
    assert _rewrite_text(expected, tag) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Honest gap (dojo 0.14.0): field_presence is unemitted.\n",
        "v0.14.0 added citation / inclusion / ground_truth.\n",
        "| llm-entity-extraction | llm-dojo-scoring | git pin `@v0.16.0` "
        "(workspace source in the monorepo) |\n",
        "entity-extraction and agent-mailroom pin v0.16.0\n",
        "![Release](https://img.shields.io/badge/release-v0.7.1-2EA043)\n",
        '<img alt="Other library v0.18.0">\n',
        'assert "other-library.git@v0.18.0" in pin\n',
    ],
    ids=["empty", "honesty-gap", "history", "sibling-table", "sibling-prose",
         "release-badge", "unrelated-alt", "unrelated-dependency"],
)
def test_rewrite_preserves_non_mailroom_version_references(text):
    assert _rewrite_text(text, "v0.20.0") == text


def test_rewrite_updates_all_occurrences_without_changing_surrounding_text():
    before = (
        "文 — llm-mailroom pins v0.18.0; llm-mailroom pins 0.19.1\n"
        'assert "llm-dojo-scoring.git@0.18.0" in pin\n'
        "entity-extraction and agent-mailroom pin v0.16.0\n"
    )
    expected = (
        "文 — llm-mailroom pins v0.20.0; llm-mailroom pins v0.20.0\n"
        'assert "llm-dojo-scoring.git@v0.20.0" in pin\n'
        "entity-extraction and agent-mailroom pin v0.16.0\n"
    )
    assert _rewrite_text(before, "v0.20.0") == expected


@pytest.mark.parametrize("tag", ["v0.20.0-rc.1", "0.20.0-rc.1"])
def test_rewrite_prerelease_dependency_pin_is_not_rewritten_twice(tag):
    """The short git-pin matcher must not duplicate an already replaced suffix."""
    dependency = (
        "llm-dojo-scoring @ "
        "git+https://github.com/Exios66/llm-dojo-scoring.git@"
    )
    expected = dependency + "v0.20.0-rc.1"

    assert _rewrite_text(dependency + "v0.19.1", tag) == expected
    assert _rewrite_text(expected, tag) == expected


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/README.md",
        "docs/start-here/overview.md",
        "docs/how-it-fits-together/architecture.md",
        "docs/repository-guides/repos/llm-dojo-scoring.md",
        "docs/pipeline-reference-llm-mailroom/sister-repos.md",
        "docs/constellation/overview.md",
        "docs/constellation/architecture.md",
        "docs/constellation/repos/llm-dojo-scoring.md",
        "docs/the-pipeline-in-depth/scoring-and-metrics.md",
        "docs/the-pipeline-in-depth/running.md",
        "docs/pipeline-reference-llm-mailroom/architecture.md",
        "docs/architecture.md",
        "landing/index.html",
    ],
)
def test_apply_pin_includes_new_published_surfaces(tmp_path, relative_path):
    """Exercise file discovery independently of the production PIN_FILES list."""
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    before = (
        '<img src="https://img.shields.io/badge/dojo-v0.18.0-6f42c1" '
        'alt="llm-dojo-scoring v0.18.0">\n'
        "文 — entity-extraction and agent-mailroom pin v0.16.0\n"
    )
    path.write_text(before, encoding="utf-8")
    historical = tmp_path / "CHANGELOG.md"
    historical.write_text(before, encoding="utf-8")
    # A listed file with no pin changes should not be reported as updated.
    unchanged = tmp_path / "README.md"
    unchanged.write_text("No dependency pins here.\n", encoding="utf-8")
    original_files = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}

    assert apply_pin("v0.20.0", root=tmp_path, dry_run=True) == [path]
    assert path.read_text(encoding="utf-8") == before
    assert apply_pin("0.20.0", root=tmp_path) == [path]
    assert path.read_text(encoding="utf-8") == before.replace("v0.18.0", "v0.20.0")
    assert apply_pin("v0.20.0", root=tmp_path) == []
    assert historical.read_text(encoding="utf-8") == before
    assert unchanged.read_text(encoding="utf-8") == "No dependency pins here.\n"
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()} == original_files


@pytest.mark.parametrize("old_tag", ["v0.19.1", "v0.22.0-rc.1", "v0.20.0.rc.1"])
@pytest.mark.parametrize("tag", ["v0.20.0", "0.20.0-rc.2"])
@pytest.mark.parametrize(
    "template",
    [
        "@git+https://github.com/Exios66/llm-dojo-scoring.git@{version}",
        "Pin: `llm-dojo-scoring @ git+https://github.com/Exios66/"
        "llm-dojo-scoring.git@{version}`",
        '[![Dojo](https://img.shields.io/badge/dojo-{version}-6f42c1)]'
        '(https://github.com/Exios66/llm-dojo-scoring/releases/tag/{version})',
        '<a href="https://github.com/Exios66/llm-dojo-scoring/releases/tag/{version}">'
        '<img src="https://img.shields.io/badge/dojo-{version}-6f42c1" '
        'alt="llm-dojo-scoring {version}"></a>',
        "| llm-mailroom | llm-dojo-scoring | git pin `@{version}`, auto-bumped |\n",
        "| llm-mailroom | llm-dojo-scoring | git pin `@{version}` as of "
        "2026-10-07, auto-bumped |\n",
        "| llm-mailroom | llm-dojo-scoring | git pin `@{version}`, "
        "auto-bumped (as of 2026-10-07) |\n",
        "PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply "
        "--tag {version} --allow-missing-release\n",
    ],
)
def test_rewrite_replaces_full_pin_in_urls_badges_and_alt_text(old_tag, tag, template):
    expected = template.format(version=_normalize_tag(tag))
    assert _rewrite_text(template.format(version=old_tag), tag) == expected
    assert _rewrite_text(expected, tag) == expected


@pytest.mark.parametrize("relative_path", ["README.md", "docs/README.md", "landing/index.html"])
def test_apply_pin_keeps_badge_and_release_destination_in_sync(tmp_path, relative_path):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    template = (
        '[![Dojo](https://img.shields.io/badge/dojo-{version}-6f42c1)]'
        '(https://github.com/Exios66/llm-dojo-scoring/releases/tag/{version})\n'
        '<a href="https://github.com/Exios66/llm-dojo-scoring/releases/tag/{version}">'
        '<img src="https://img.shields.io/badge/dojo-{version}-6f42c1" '
        'alt="llm-dojo-scoring {version}"></a>\n'
        'https://github.com/Exios66/llm-mailroom/releases/tag/v0.7.1\n'
        'https://github.com/Other/llm-dojo-scoring/releases/tag/v0.19.1\n'
    )
    path.write_text(template.format(version="v0.19.1"), encoding="utf-8")
    for tag in ("v0.20.0-rc.1", "v0.20.0-rc.2", "v0.20.0"):
        assert apply_pin(tag, root=tmp_path) == [path]
        assert path.read_text(encoding="utf-8") == template.format(version=tag)
        assert apply_pin(tag, root=tmp_path) == []
