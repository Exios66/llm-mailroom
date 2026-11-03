"""Network-free tests for src/scripts/release.py (the version-bump / release governor).

Mutating paths run against a throwaway git repo in ``tmp_path`` that carries
every file the tool edits plus the real GitBook changelog mirror script;
``REPO_ROOT`` is monkeypatched to it. ``origin``/``gh`` are never contacted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import release as R

REAL_ROOT = Path(__file__).resolve().parents[2]
OLD = "0.1.1"

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Something new.

## [v0.1.1] - 2026-01-02

### Fixed

- A fix.

## [v0.1.0] - 2026-01-01

### Added

- First.

[Unreleased]: https://github.com/Exios66/llm-mailroom/compare/v0.1.1...HEAD
[v0.1.1]: https://github.com/Exios66/llm-mailroom/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/Exios66/llm-mailroom/releases/tag/v0.1.0
"""

BADGE = "https://img.shields.io/badge/release-v{v}-2EA043"
TAG_URL = "https://github.com/Exios66/llm-mailroom/releases/tag/v{v}"
FILES = {
    "pyproject.toml": '[project]\nname = "mailroom"\nversion = "{v}"\n',
    "README.md": f"[![Release]({BADGE})]({TAG_URL})\n[`v{{v}}`]({TAG_URL}).\n",
    "landing/index.html": (
        f'<a href="{TAG_URL}"><img src="{BADGE}" alt="Release v{{v}}"></a>\n'
        "<em>release</em> · v{v}\n"
    ),
    "docs/README.md": f"[![Release]({BADGE})]({TAG_URL}) [**release** · v{{v}}](x)\n",
    "docs/constellation/overview.md": "| llm-mailroom | v{v} |\n",
    "docs/start-here/overview.md": "| llm-mailroom     | v{v}      |\n",
    "docs/constellation/repos/llm-mailroom.md": "| Release | v{v} |\n",
    "docs/repository-guides/repos/llm-mailroom.md": "| Release       | v{v}   |\n",
    "docs/the-pipeline-in-depth/running.md": "| version | `mailroom` {v} | pyproject |\n",
    "docs/the-pipeline-in-depth/scoring-and-metrics.md": (
        "no scorecard for the current release ({v}).\nNothing for release {v}.\n"
    ),
    "src/tests/test_landing.py": 'BADGES = ("release-v{v}-2EA043",)\n',
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A consistent fake repo at 0.1.1 (committed, tagged v0.1.1, GitBook mirror generated)."""
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text.format(v=OLD), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "src/scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL_ROOT / R.SYNC_SCRIPT, tmp_path / R.SYNC_SCRIPT)
    (tmp_path / "docs/changelog").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(R, "REPO_ROOT", tmp_path)
    assert R.run_sync(check=False).returncode == 0
    _git(tmp_path, "init", "-q", "-b", "main")
    for key, val in (("user.name", "t"), ("user.email", "t@example.com"),
                     ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        _git(tmp_path, "config", key, val)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "tag", "-a", "v0.1.1", "-m", "Release v0.1.1")
    return tmp_path


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def cut(version="0.2.0", *args):
    return R.main(["cut", version, "--date", "2026-02-03", *args])


# --- status / check ---------------------------------------------------------


def test_status_json_reports_identity_and_next(repo, capsys):
    assert R.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == OLD and out["newest_tag"] == "v0.1.1"
    assert out["unreleased_empty"] is False
    assert isinstance(out["next"], list) and any("cut" in step for step in out["next"])


def test_check_passes_on_consistent_repo(repo):
    errors, warnings = R.check_chain(R.chain_state())
    assert errors == [] and warnings == []
    assert R.main(["check"]) == 0


@pytest.mark.parametrize(
    "rel,old,new,needle",
    [
        ("README.md", "release-v0.1.1", "release-v0.1.0", "README.md"),
        ("docs/the-pipeline-in-depth/running.md", "0.1.1", "0.0.9", "running.md"),
        ("CHANGELOG.md", "compare/v0.1.1...HEAD", "compare/v0.1.0...HEAD", "[Unreleased] compares"),
        ("CHANGELOG.md", "[v0.1.1]: ", "[v0.1.1-x]: ", "no [v0.1.1] link"),
        ("pyproject.toml", 'version = "0.1.1"', 'version = "0.1.0"', "newest CHANGELOG section"),
    ],
)
def test_check_reports_drift(repo, rel, old, new, needle):
    path = repo / rel
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    errors, _ = R.check_chain(R.chain_state())
    assert any(needle in e for e in errors), errors
    assert R.main(["check"]) == 1


def test_check_reports_stale_gitbook_mirror(repo):
    (repo / "docs/changelog/unreleased.md").write_text("stale\n", encoding="utf-8")
    errors, _ = R.check_chain(R.chain_state())
    assert any("docs/changelog/ is stale" in e for e in errors)


def test_check_flags_tag_ahead_of_package_and_lightweight_tags(repo):
    _git(repo, "tag", "v0.3.0")
    errors, warnings = R.check_chain(R.chain_state())
    assert any("behind newest tag v0.3.0" in e for e in errors)
    assert any("v0.3.0 is lightweight" in w for w in warnings)
    assert any("v0.3.0 has no matching CHANGELOG" in w for w in warnings)


# --- cut --------------------------------------------------------------------


def test_cut_dry_run_writes_nothing_and_reports_plan(repo, capsys):
    before = snapshot(repo)
    assert cut("0.2.0", "--json") == 0
    plan = json.loads(capsys.readouterr().out)
    assert snapshot(repo) == before
    assert plan["dry_run"] is True and plan["tag"] == "v0.2.0" and plan["from_version"] == OLD
    assert {c["path"] for c in plan["changes"]} >= set(R.VERSION_LOCATIONS) | {"CHANGELOG.md"}
    assert plan["next"] and "--apply" in plan["next"][0]


def test_cut_apply_moves_every_location_and_chain_stays_green(repo):
    assert cut("0.2.0", "--apply") == 0
    changelog = (repo / "CHANGELOG.md").read_text()
    assert re.search(r"## \[Unreleased\]\n\n## \[v0\.2\.0\] - 2026-02-03\n\n### Added\n\n- Something new\.\n\n## \[v0\.1\.1\]", changelog)
    assert "[Unreleased]: https://github.com/Exios66/llm-mailroom/compare/v0.2.0...HEAD\n[v0.2.0]: https://github.com/Exios66/llm-mailroom/compare/v0.1.1...v0.2.0\n[v0.1.1]:" in changelog
    for rel in R.VERSION_LOCATIONS:
        text = (repo / rel).read_text()
        assert "0.2.0" in text and OLD not in text.replace("release 0.1.1", ""), rel
    assert R.pyproject_version() == "0.2.0"
    # Not yet tagged: only the "tag is not local yet" warning, no errors (incl. GitBook mirror).
    errors, warnings = R.check_chain(R.chain_state())
    assert errors == []
    assert warnings == ["tag v0.2.0 is not local yet (publish creates it)"]
    assert (repo / "docs" / "changelog" / "2026" / "v0-2-0.md").is_file()


def test_cut_warns_about_mentions_it_does_not_move(repo, capsys):
    cut("0.2.0", "--json")
    plan = json.loads(capsys.readouterr().out)
    assert plan["warnings"] == [
        "docs/the-pipeline-in-depth/scoring-and-metrics.md:2 still mentions 0.1.1 — not moved by cut, review"
    ]


@pytest.mark.parametrize(
    "version,match",
    [("0.1.1", "tag v0.1.1 already exists"), ("0.0.9", "must be greater"), ("0.1.0", "already has"), ("1.0", "not semver")],
)
def test_cut_refuses_bad_versions(repo, capsys, version, match):
    assert cut(version) == 1
    assert match in capsys.readouterr().err


def test_cut_refuses_version_below_newest_tag(repo, capsys):
    _git(repo, "tag", "v0.5.0")
    assert cut("0.2.0") == 1
    assert "must be greater than 0.5.0" in capsys.readouterr().err


def test_cut_refuses_dirty_tree_and_empty_unreleased(repo, capsys):
    (repo / "scratch.txt").write_text("x")
    assert cut() == 1 and "dirty" in capsys.readouterr().err
    assert cut("0.2.0", "--allow-dirty") == 0
    (repo / "scratch.txt").unlink()
    text = (repo / "CHANGELOG.md").read_text().replace("### Added\n\n- Something new.\n\n", "", 1)
    (repo / "CHANGELOG.md").write_text(text)
    _git(repo, "commit", "-qam", "empty unreleased")
    assert cut() == 1 and "[Unreleased] is empty" in capsys.readouterr().err
    assert cut("0.2.0", "--allow-empty", "--apply") == 0
    assert "## [v0.2.0] - 2026-02-03\n\n## [v0.1.1]" in (repo / "CHANGELOG.md").read_text()


def test_cut_refuses_bad_date(repo, capsys):
    assert R.main(["cut", "0.2.0", "--date", "yesterday"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_cut_is_all_or_nothing_when_a_location_cannot_be_moved(repo, capsys):
    (repo / "README.md").write_text("no release badge here\n")
    _git(repo, "commit", "-qam", "break readme")
    before = snapshot(repo)
    assert cut("0.2.0", "--apply") == 1
    assert "README.md: no match" in capsys.readouterr().err
    assert snapshot(repo) == before


# --- notes ------------------------------------------------------------------


def _merge_pr(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("f")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feature work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #7 from o/feature", "-m", "Add the feature")


def test_notes_render_section_prs_and_compare(repo, capsys, tmp_path):
    _merge_pr(repo)
    cut("0.2.0", "--apply")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release 0.2.0")
    out = tmp_path.parent / "notes-out.md"
    assert R.main(["notes", "0.2.0", "--out", str(out)]) == 0
    body = out.read_text()
    assert body.startswith("# llm-mailroom v0.2.0\n")
    assert "- Something new." in body and "- Add the feature — #7" in body
    assert "compare/v0.1.1...v0.2.0" in body
    assert f"`{_git(repo, 'rev-parse', 'HEAD').strip()}`" in body
    capsys.readouterr()
    assert R.main(["notes", "0.2.0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["previous"] == "v0.1.1"
    assert R.main(["notes", "9.9.9"]) == 1


# --- publish ----------------------------------------------------------------


@pytest.fixture
def released(repo, monkeypatch):
    """repo after cut 0.2.0 + commit; origin has no tag and no release."""
    cut("0.2.0", "--apply")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release 0.2.0")
    calls: list[list[str]] = []
    monkeypatch.setattr(R, "remote_tag_commit", lambda tag: None)
    monkeypatch.setattr(R, "remote_release", lambda tag: None)
    real_run = R.run

    def fake_run(cmd, *, check=True):
        if cmd[0] == "gh" or cmd[:2] == ["git", "push"]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, check=check)

    monkeypatch.setattr(R, "run", fake_run)
    return repo, calls


def test_publish_dry_run_plans_but_changes_nothing(released, capsys):
    repo, calls = released
    before = _git(repo, "tag", "--list")
    assert R.main(["publish", "0.2.0", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True and plan["errors"] == []
    assert [s["op"] for s in plan["steps"]] == ["git_tag"]  # no release until the tag is on origin
    assert any("--push" in n for n in plan["next"])
    assert _git(repo, "tag", "--list") == before and calls == []


def test_publish_apply_creates_annotated_tag_only_without_push(released):
    repo, calls = released
    assert R.main(["publish", "0.2.0", "--apply"]) == 0
    assert _git(repo, "for-each-ref", "refs/tags/v0.2.0", "--format=%(objecttype) %(contents:subject)").strip() == "tag Release v0.2.0"
    assert _git(repo, "rev-list", "-n1", "v0.2.0") == _git(repo, "rev-parse", "HEAD")
    assert calls == []


def test_publish_push_pushes_plain_tag_then_creates_release(released, monkeypatch):
    repo, calls = released
    monkeypatch.setattr(R, "remote_release", lambda tag: {"url": "https://example/rel"} if calls and calls[-1][0] == "gh" else None)
    assert R.main(["publish", "0.2.0", "--apply", "--push"]) == 0
    assert calls[0] == ["git", "push", "origin", "refs/tags/v0.2.0"]
    gh = calls[1]
    assert gh[:3] == ["gh", "release", "create"] and "--verify-tag" in gh
    assert gh[gh.index("--title") + 1] == "llm-mailroom v0.2.0"
    assert not any(f in " ".join(c) for c in calls for f in ("--force", "-f ", "--delete"))


def test_publish_refusals(released, monkeypatch, capsys):
    repo, _ = released
    sha = _git(repo, "rev-parse", "HEAD").strip()
    other = _git(repo, "rev-parse", "HEAD~1").strip()

    def plan(**kw):
        capsys.readouterr()
        R.main(["publish", kw.pop("version", "0.2.0"), "--json", *kw.pop("args", [])])
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(R, "remote_release", lambda tag: {"url": "u", "isDraft": False})
    assert any("already exists" in e for e in plan()["errors"])
    monkeypatch.setattr(R, "remote_release", lambda tag: None)

    monkeypatch.setattr(R, "remote_tag_commit", lambda tag: other)
    assert any("origin v0.2.0 points at" in e for e in plan()["errors"])
    monkeypatch.setattr(R, "remote_tag_commit", lambda tag: sha)
    assert plan()["errors"] == [] and "gh_release_create" in [s["op"] for s in plan()["steps"]]
    monkeypatch.setattr(R, "remote_tag_commit", lambda tag: None)

    assert any("pyproject version 0.1.1 != 0.2.0" in e for e in plan(args=["--commit", "HEAD~1"])["errors"])
    _git(repo, "tag", "-a", "v0.2.0", "HEAD~1", "-m", "Release v0.2.0")
    assert any("local tag v0.2.0 points at" in e for e in plan()["errors"])
    assert R.main(["publish", "0.2.0", "--apply"]) == 1  # errors block apply
    assert R.main(["publish", "0.3.0"]) == 1  # no section for it


def test_publish_chain_errors_block_head_but_only_warn_for_older_commits(released, capsys):
    repo, _ = released
    (repo / "README.md").write_text((repo / "README.md").read_text().replace("v0.2.0", "v0.1.0"))
    _git(repo, "commit", "-qam", "drift")
    R.main(["publish", "0.2.0", "--json"])
    assert any("README.md" in e for e in json.loads(capsys.readouterr().out)["errors"])
    R.main(["publish", "0.2.0", "--json", "--commit", "HEAD~1"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["errors"] == [] and any(w.startswith("working tree:") for w in plan["warnings"])


def test_unreachable_origin_is_a_refusal_not_a_silent_none(repo, capsys):
    assert R.main(["publish", "0.1.1", "--json"]) == 1
    assert "cannot query origin" in capsys.readouterr().err


# --- guards / CLI -----------------------------------------------------------


def test_usage_error_exits_2():
    with pytest.raises(SystemExit) as exc:
        R.main(["cut"])
    assert exc.value.code == 2


def test_tool_never_forces_deletes_or_amends():
    source = Path(R.__file__).read_text(encoding="utf-8")
    for forbidden in ("--force", "push -f", "tag -d", '"-d"', "--delete", "--amend", "reset --hard"):
        assert forbidden not in source, forbidden


# --- the real repository ----------------------------------------------------


def test_real_repo_version_locations_agree_with_pyproject():
    """Every documented 'current release' string equals pyproject's version (tool and docs stay in lockstep)."""
    assert R.REPO_ROOT == REAL_ROOT
    assert R.location_errors(R.pyproject_version()) == []


def test_real_repo_changelog_parses_with_unreleased_first():
    sections = R.parse_changelog(R.changelog_text())
    assert sections[0]["is_unreleased"]
    assert sections[1]["version"] == R.pyproject_version()
