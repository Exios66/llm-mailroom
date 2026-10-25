"""Offline tests for src/scripts/bump_dojo_scoring.py rewrite helpers."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import bump_dojo_scoring as B
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
            "| llm-mailroom | llm-dojo-scoring | git pin `@{version}`; "
            "changed through explicit release adoption |\n",
            id="explicit-adoption-table",
        ),
        pytest.param(
            "| llm-mailroom | llm-dojo-scoring | git pin `@{version}` "
            "as of 2026-10-23; changed through explicit release adoption |\n",
            id="dated-explicit-adoption-table",
        ),
        pytest.param("pinned release `{version}`", id="pinned-release-prose"),
        pytest.param("In the pinned {version}, bands are read", id="pinned-library-prose"),
        pytest.param("Dojo pin, **{version}**.", id="checkout-pin-prose"),
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
    """Keep badge labels and release links in sync without changing unrelated links."""
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


# --- interim commit-SHA pins -------------------------------------------------

SHA = "8c7b7f1c9c3b18a7036ee58addcfaa35d816177d"


def test_normalize_accepts_full_sha_only():
    """Normalize full commit SHAs and reject abbreviated or malformed values."""
    assert _normalize_tag(SHA) == SHA
    assert _normalize_tag(SHA.upper()) == SHA
    for bad in (SHA[:7], SHA + "0", "g" * 40):
        with pytest.raises(ValueError):
            _normalize_tag(bad)


def test_pin_re_and_current_pin_read_a_sha(tmp_path: Path):
    """Read a full commit pin through both the regex and pyproject reader."""
    line = f'    "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@{SHA}",'
    assert PIN_RE.search(line).group(2) == SHA
    (tmp_path / "pyproject.toml").write_text(f"dependencies = [\n{line}\n]\n", encoding="utf-8")
    assert current_pin(tmp_path) == SHA


@pytest.mark.parametrize("old", ["v0.21.0", SHA])
def test_rewrite_to_sha_and_back_to_tag_is_lossless(old):
    """Round-trip recognized pins through a SHA while preserving sibling pins."""
    template = (
        "dependency: llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@{ref}\n"
        "| llm-mailroom pins {ref}; entity-extraction pin v0.16.0 |\n"
        "| llm-mailroom | llm-dojo-scoring | git pin `@{ref}`, auto-bumped |\n"
        "pinned at **{ref}** in `pyproject.toml`\n"
        "the pipeline pins llm-dojo-scoring **{ref}**.\n"
        "[src](https://github.com/Exios66/llm-dojo-scoring/blob/{ref}/llm_dojo_scoring/config.py)\n"
        "assert \"llm-dojo-scoring.git@{ref}\" in pin\n"
    )
    to_sha = _rewrite_text(template.format(ref=old), SHA)
    assert to_sha == template.format(ref=SHA)
    assert _rewrite_text(to_sha, SHA) == to_sha
    to_tag = _rewrite_text(to_sha, "v0.22.0")
    assert to_tag == template.format(ref="v0.22.0")
    assert "v0.16.0" in to_tag  # sibling pins untouched


def test_sha_badge_uses_short_label_and_commit_link_and_back():
    """Switch badge labels and destinations between release tags and commit pins."""
    badge = (
        '<a href="https://github.com/Exios66/llm-dojo-scoring/{path}">'
        '<img src="https://img.shields.io/badge/dojo-{label}-6f42c1" '
        'alt="llm-dojo-scoring {label}"></a>'
    )
    tag_form = badge.format(path="releases/tag/v0.21.0", label="v0.21.0")
    sha_form = badge.format(path=f"commit/{SHA}", label=SHA[:7])
    assert _rewrite_text(tag_form, SHA) == sha_form
    assert _rewrite_text(sha_form, SHA) == sha_form
    assert _rewrite_text(sha_form, "v0.22.0") == badge.format(
        path="releases/tag/v0.22.0", label="v0.22.0"
    )


def test_interim_sha_relabels_pinned_release_as_commit_and_back():
    """A SHA target must not present an unreleased commit as a release."""
    assert _rewrite_text("pinned release `v0.18.0`", SHA) == f"pinned commit `{SHA}`"
    assert _rewrite_text(f"pinned commit `{SHA}`", "v0.20.0") == "pinned release `v0.20.0`"


def test_main_never_downgrades_an_interim_sha_pin(monkeypatch, capsys):
    """Treat an older release as up to date for checks and refuse to apply it."""
    from scripts import bump_dojo_scoring as B

    monkeypatch.setattr(B, "current_pin", lambda root=B.REPO_ROOT: SHA)
    monkeypatch.setattr(B, "release_exists", lambda tag, repo=B.DOJO_REPO: True)
    monkeypatch.setattr(B, "tag_contains_commit", lambda sha, tag, repo=B.DOJO_REPO: False)
    # The workflow's backstop (--check) must read "nothing to bump", not exit 2 ...
    assert B.main(["--check", "--tag", "v0.21.0"]) == 0
    # ... and an explicit --apply must refuse, writing nothing.
    monkeypatch.setattr(B, "apply_pin", lambda *a, **k: pytest.fail("must not write"))
    assert B.main(["--apply", "--tag", "v0.21.0"]) == 1
    # A tag that contains the commit is a normal bump.
    monkeypatch.setattr(B, "tag_contains_commit", lambda sha, tag, repo=B.DOJO_REPO: True)
    assert B.main(["--check", "--tag", "v0.22.0"]) == 2


def test_release_commit_resolves_tag_to_commit(monkeypatch):
    """Resolve a release tag ref to the exact commit GitHub publishes."""
    seen = {}

    class _Resp:
        def read(self):
            return json.dumps({"sha": SHA}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=30):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(B.urllib.request, "urlopen", fake_urlopen)
    assert B.release_commit("v0.24.2") == SHA
    assert seen["url"].endswith("/commits/v0.24.2")


def test_main_keeps_sha_pin_that_is_the_release_commit(monkeypatch, capsys):
    """An immutable SHA pin at the release commit is already up to date."""
    monkeypatch.setattr(B, "current_pin", lambda root=B.REPO_ROOT: SHA)
    monkeypatch.setattr(B, "release_exists", lambda tag, repo=B.DOJO_REPO: True)
    monkeypatch.setattr(B, "release_commit", lambda tag, repo=B.DOJO_REPO: SHA)
    monkeypatch.setattr(B, "tag_contains_commit", lambda sha, tag, repo=B.DOJO_REPO: True)
    monkeypatch.setattr(
        B,
        "apply_pin",
        lambda *a, **k: pytest.fail("must not rewrite an immutable release-commit pin"),
    )

    assert B.main(["--check", "--tag", "v0.24.2"]) == 0
    assert B.main(["--apply", "--tag", "v0.24.2"]) == 0
    assert "immutable_pin=true" in capsys.readouterr().out


def test_allow_missing_release_cannot_replace_immutable_sha_with_tag(monkeypatch, capsys):
    """Offline mode must not trade an immutable pin for a movable tag.

    ``--allow-missing-release`` skips the release verification that proves the
    tag names the current commit. Without that proof, rewriting the SHA would
    silently change the install contract.
    """
    monkeypatch.setattr(B, "current_pin", lambda root=B.REPO_ROOT: SHA)
    monkeypatch.setattr(
        B,
        "apply_pin",
        lambda *a, **k: pytest.fail("must not rewrite an immutable SHA pin to a tag"),
    )

    assert B.main(["--apply", "--tag", "v0.24.2", "--allow-missing-release"]) == 1
    assert "refusing to replace an immutable SHA pin" in capsys.readouterr().err


def test_main_accepts_sha_target_contained_in_latest_release(monkeypatch):
    """A SHA target is validated against the latest release, not releases/tags."""
    written = []
    monkeypatch.setattr(B, "current_pin", lambda root=B.REPO_ROOT: "v0.24.1")
    monkeypatch.setattr(
        B, "release_exists", lambda *a, **k: pytest.fail("SHA targets are not release tags")
    )
    monkeypatch.setattr(B, "latest_release_tag", lambda repo=B.DOJO_REPO: "v0.24.2")
    monkeypatch.setattr(
        B,
        "tag_contains_commit",
        lambda sha, tag, repo=B.DOJO_REPO: sha == SHA and tag == "v0.24.2",
    )
    monkeypatch.setattr(
        B, "apply_pin", lambda ref, **k: written.append(ref) or [B.REPO_ROOT / "pyproject.toml"]
    )

    assert B.main(["--apply", "--tag", SHA]) == 0
    assert written == [SHA]


def test_main_rejects_sha_target_absent_from_latest_release(monkeypatch):
    """Refuse to pin a commit that no published release contains."""
    monkeypatch.setattr(B, "current_pin", lambda root=B.REPO_ROOT: "v0.24.1")
    monkeypatch.setattr(B, "latest_release_tag", lambda repo=B.DOJO_REPO: "v0.24.2")
    monkeypatch.setattr(B, "tag_contains_commit", lambda sha, tag, repo=B.DOJO_REPO: False)
    monkeypatch.setattr(
        B, "apply_pin", lambda *a, **k: pytest.fail("must not write an unpublished commit")
    )

    assert B.main(["--apply", "--tag", SHA]) == 1


@pytest.mark.parametrize(
    ("ref", "expected"),
    [(SHA, True), ("0" * 40, True), (SHA.upper(), False),
     (SHA[:39], False), (SHA + "0", False), ("g" * 40, False),
     ("v0.22.0", False), (None, False)],
)
def test_is_sha_requires_exactly_40_lowercase_hex_characters(ref, expected):
    assert B.is_sha(ref) is expected


@pytest.mark.parametrize(
    ("ref", "label", "path"),
    [
        (f"  {SHA.upper()}\n", SHA[:7], f"commit/{SHA}"),
        ("0" * 40, "0" * 7, "commit/" + "0" * 40),
        (" 0.22.0-rc.1 ", "v0.22.0-rc.1", "releases/tag/v0.22.0-rc.1"),
    ],
)
def test_pin_presentation_normalizes_refs(ref, label, path):
    assert B.pin_label(ref) == label
    assert B.pin_release_path(ref) == path


@pytest.mark.parametrize("ref", [SHA[:7], SHA + "0", "main", " "])
def test_invalid_target_ref_never_writes_files(tmp_path, ref):
    path = tmp_path / "README.md"
    before = "llm-mailroom pins v0.21.0\n"
    path.write_text(before, encoding="utf-8")
    with pytest.raises(ValueError):
        apply_pin(ref, root=tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_rewrite_to_sha_preserves_package_version_assertions():
    """A commit identifies source bytes, not a new installed package version."""
    version_checks = (
        '"""llm-dojo-scoring v0.21.0 pin"""\n'
        'assert llm_dojo_scoring.__version__ == "0.21.0"\n'
        "def test_installed_dojo_is_v0210():\n    pass\n"
    )
    before = f"llm-mailroom pins v0.21.0\n{version_checks}"
    assert _rewrite_text(before, SHA) == f"llm-mailroom pins {SHA}\n{version_checks}"


@pytest.mark.parametrize(
    ("template", "short_label"),
    [
        ("[`llm-dojo-scoring`](https://example.test/scoring) ({ref}).", True),
        ("`llm-dojo-scoring` ({ref}).", True),
        ("llm-dojo-scoring `field_scoring.py` ({ref})", True),
        ("the pinned scoring engine, `{ref}`", False),
        ("Pinned as a git dependency (`@{ref}`)", False),
        ("bump_dojo_scoring.py --apply --tag {ref}", False),
        ("pinned `@{ref}`", False),
        ("llm-dojo-scoring pin and mailroom scoring suites ({ref})", False),
    ],
)
def test_rewrite_documented_sha_surfaces_and_restore_release(template, short_label):
    before = template.format(ref="v0.21.0")
    expected = template.format(ref=SHA[:7] if short_label else SHA)
    assert _rewrite_text(before, SHA) == expected
    assert _rewrite_text(expected, SHA) == expected
    assert _rewrite_text(expected, "v0.22.0") == template.format(ref="v0.22.0")


@pytest.fixture
def github_urlopen(mocker):
    """Stub the HTTP boundary; the request and response parsing stay real."""
    urlopen = mocker.patch.object(B.urllib.request, "urlopen")
    urlopen.return_value.__enter__.return_value.status = 200
    return urlopen


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"status": "ahead"}, True), ({"status": "identical"}, True),
     ({"status": "behind"}, False), ({"status": "diverged"}, False),
     ({"status": "unknown"}, False), ({"status": None}, False), ({}, False)],
)
def test_tag_contains_commit_checks_ancestry_and_normalizes_request(
    github_urlopen, payload, expected,
):
    github_urlopen.return_value.__enter__.return_value.read.return_value = (
        json.dumps(payload).encode("utf-8")
    )
    assert B.tag_contains_commit(SHA.upper(), "0.22.0", repo="example/scoring") is expected
    request = github_urlopen.call_args.args[0]
    assert request.full_url == (
        f"https://api.github.com/repos/example/scoring/compare/{SHA}...v0.22.0"
    )
    assert github_urlopen.call_args.kwargs == {"timeout": 30}
    assert github_urlopen.call_count == 1


@pytest.mark.parametrize("code", [404, 403, 500])
def test_tag_contains_commit_http_errors_are_not_treated_as_ancestry(github_urlopen, code):
    error = HTTPError("https://example.test/compare", code, "lookup failed", None, None)
    github_urlopen.side_effect = error
    with pytest.raises(RuntimeError, match=f"GitHub compare failed: {code}") as exc:
        B.tag_contains_commit(SHA, "v0.22.0")
    assert exc.value.__cause__ is error


def test_tag_contains_commit_propagates_network_and_json_failures(github_urlopen):
    github_urlopen.side_effect = URLError("offline")
    with pytest.raises(URLError, match="offline"):
        B.tag_contains_commit(SHA, "v0.22.0")
    github_urlopen.side_effect = None
    github_urlopen.return_value.__enter__.return_value.read.return_value = b"not JSON"
    with pytest.raises(json.JSONDecodeError):
        B.tag_contains_commit(SHA, "v0.22.0")


@pytest.mark.parametrize(("sha", "tag"), [(SHA[:7], "v0.22.0"), (SHA, "latest")])
def test_tag_contains_commit_rejects_invalid_refs_before_http(github_urlopen, sha, tag):
    with pytest.raises(ValueError):
        B.tag_contains_commit(sha, tag)
    github_urlopen.assert_not_called()


@pytest.fixture
def interim_pin_cli(tmp_path, monkeypatch, mocker):
    """Run the real CLI rewrites in a tiny repo with all GitHub calls mocked."""
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["llm-dojo-scoring @ '
        f'git+https://github.com/Exios66/llm-dojo-scoring.git@{SHA}"]\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(f"llm-mailroom pins {SHA}\n", encoding="utf-8")
    monkeypatch.setattr(B, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(B, "current_pin", lambda: current_pin(tmp_path))
    mocker.patch.object(
        B, "apply_pin", side_effect=lambda tag, **kw: apply_pin(tag, root=tmp_path, **kw)
    )
    mocker.patch.object(B, "latest_release_tag", return_value="v0.22.0")
    mocker.patch.object(B, "release_exists", return_value=True)
    mocker.patch.object(B, "tag_contains_commit", return_value=True)
    return tmp_path


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_main_promotes_interim_pin_to_containing_release(interim_pin_cli, dry_run, capsys):
    args = ["--apply", "--tag", "0.22.0"] + (["--dry-run"] if dry_run else [])
    assert B.main(args) == 0
    expected_pin = SHA if dry_run else "v0.22.0"
    assert current_pin(interim_pin_cli) == expected_pin
    assert (interim_pin_cli / "README.md").read_text() == f"llm-mailroom pins {expected_pin}\n"
    B.release_exists.assert_called_once_with("v0.22.0")
    B.tag_contains_commit.assert_called_once_with(SHA, "v0.22.0")
    B.latest_release_tag.assert_not_called()
    assert f"changed=2 dry_run={dry_run}" in capsys.readouterr().out


@pytest.mark.parametrize(("mode", "expected"), [("--check", 0), ("--apply", 1)])
def test_main_latest_release_cannot_downgrade_interim_pin(interim_pin_cli, mode, expected):
    B.tag_contains_commit.return_value = False
    assert B.main([mode]) == expected
    B.latest_release_tag.assert_called_once_with()
    B.tag_contains_commit.assert_called_once_with(SHA, "v0.22.0")
    B.apply_pin.assert_not_called()
    assert current_pin(interim_pin_cli) == SHA


@pytest.mark.parametrize("mode", ["--check", "--apply"])
def test_main_missing_release_leaves_interim_pin_untouched(interim_pin_cli, mode, capsys):
    B.release_exists.return_value = False
    assert B.main([mode, "--tag", "v0.22.0"]) == 1
    B.tag_contains_commit.assert_not_called()
    B.apply_pin.assert_not_called()
    assert current_pin(interim_pin_cli) == SHA
    assert "no GitHub release for v0.22.0" in capsys.readouterr().err


def test_main_offline_override_bypasses_github_guards_for_sha_target(interim_pin_cli):
    target = "a" * 40
    assert B.main(["--apply", "--tag", target, "--allow-missing-release"]) == 0
    B.release_exists.assert_not_called()
    B.tag_contains_commit.assert_not_called()
    B.latest_release_tag.assert_not_called()
    assert current_pin(interim_pin_cli) == target


def test_main_offline_override_refuses_tag_target_from_sha_pin(interim_pin_cli, capsys):
    assert B.main(["--apply", "--tag", "v0.22.0", "--allow-missing-release"]) == 1
    B.apply_pin.assert_not_called()
    assert current_pin(interim_pin_cli) == SHA
    assert "refusing to replace an immutable SHA pin" in capsys.readouterr().err


def test_main_same_sha_is_up_to_date_without_ancestry_lookup(interim_pin_cli, capsys):
    assert B.main(["--check", "--tag", SHA.upper(), "--allow-missing-release"]) == 0
    B.tag_contains_commit.assert_not_called()
    B.apply_pin.assert_not_called()
    assert "up_to_date=true" in capsys.readouterr().out


def test_main_compare_failure_never_writes_pin(interim_pin_cli):
    B.tag_contains_commit.side_effect = RuntimeError("GitHub compare failed: 403")
    with pytest.raises(RuntimeError, match="GitHub compare failed: 403"):
        B.main(["--apply", "--tag", "v0.22.0"])
    B.apply_pin.assert_not_called()
    assert current_pin(interim_pin_cli) == SHA
