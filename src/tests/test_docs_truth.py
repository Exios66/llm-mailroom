"""hub#53 — docs-truth guard: every `docs/*.md` path referenced in tracked
prose must exist (or be explicitly annotated as pruned/upstream). Cheap
network-free net so a doc rewrite cannot silently dangle a new reference."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts.bump_dojo_scoring import current_pin

REPO_ROOT = Path(__file__).resolve().parents[2]

# Markdown links into OTHER repositories (the constellation guides under
# docs/constellation/ cite sibling repos' own docs/ trees) name paths that are
# not this repo's — strip them before scanning. Links back into this repo
# (Exios66/llm-mailroom) are still checked.
_EXTERNAL_REPO_LINK = re.compile(
    r"\[[^\]]*\]\(https?://github\.com/(?!Exios66/llm-mailroom/)[^)]*\)"
)


def _local_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _EXTERNAL_REPO_LINK.sub("", text)


def _referenced_doc_paths() -> set[str]:
    refs: set[str] = set()
    glob = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")
    for path in REPO_ROOT.glob("**/*"):
        if not path.is_file() or not path.suffix.lower() in {".md", ".py", ".yaml"}:
            continue
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        if ".opencode/skills" in str(path):  # vendored third-party skills (external URLs)
            continue
        try:
            text = _local_text(path)
        except OSError:
            continue
        for m in glob.finditer(text):
            ref = m.group(0).lstrip("/")
            refs.add(ref)
    return refs


def test_referenced_docs_exist_or_annotated():
    missing: list[str] = []
    for ref in sorted(_referenced_doc_paths()):
        target = REPO_ROOT / ref
        if target.exists():
            continue
        # docs/reports/* is deliberately pruned (heavy report archives live in
        # the upstream Exios66/llm-mailroom repo; the standalone repo tracks a
        # docs/reports/README.md annotation, the monorepo gitignores the dir —
        # monorepo-side adaptation mirroring the docs/examples/ skip below)
        if ref.startswith("docs/reports/"):
            continue
        # docs/examples/* is a pruned heavy asset (sample PDFs + manifest;
        # see the test skips citing 'pruned heavy asset; see upstream repo')
        if ref.startswith("docs/examples/"):
            continue
        missing.append(ref)
    assert not missing, f"dangling docs references: {missing}"


def test_v7_taxonomy_reference_removed():
    """The retired v7-taxonomy.md must not be referenced anywhere (repointed
    to docs/README.md / docs/configuration.md in hub#53)."""
    hits: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".yaml", ".md"}:
            continue
        if "node_modules" in path.parts or path.name == "test_docs_truth.py":
            continue
        if "v7-taxonomy.md" in _local_text(path):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"v7-taxonomy.md still referenced: {hits}"


_SUMMARY_LINK = re.compile(r"\[[^\]]+\]\(([^)]+\.md)\)")
_DOCS = REPO_ROOT / "docs"
def _summary_targets(summary_path: Path) -> list[str]:
    text = summary_path.read_text(encoding="utf-8")
    return _SUMMARY_LINK.findall(text)


def test_docker_and_modal_pages_cover_operator_matrix():
    docker = (
        _DOCS / "pipeline-reference-llm-mailroom" / "deployment" / "docker-deployment.md"
    ).read_text(encoding="utf-8")
    modal = (
        _DOCS / "pipeline-reference-llm-mailroom" / "deployment" / "modal-vllm.md"
    ).read_text(encoding="utf-8")
    for needle in (
        "MAILROOM_API_TOKEN",
        "litellm:v1.104.0",
        "DEFAULT_PROVIDER=litellm",
        "mailroom-vllm",
    ):
        assert needle in docker
    for needle in (
        "sandbox-vllm",
        "MODAL_VLLM_MODEL",
        "DEFAULT_PROVIDER=vllm",
        "mailroom-vllm",
    ):
        assert needle in modal


def test_current_dojo_pin_outside_changelog():
    """Live pin surfaces must follow the maintained dependency pin."""
    # docs/ is the stale GitBook mirror (moved to mailroom-documentation); not a live surface.
    pin = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f"llm-dojo-scoring.git@{current_pin(REPO_ROOT)}" in pin
    assert "llm-dojo-scoring.git@v0.18.0" not in pin

    stale = (
        "pins v0.18.0",
        "pinned `@v0.18.0`",
        "git pin `@v0.18.0`",
        "dojo-v0.18.0",
        "(dojo 0.14.0)",
        "(dojo 0.18.0)",
        "pinned scoring engine, `v0.18.0`",
        "pinned at **v0.18.0**",
        "library (v0.18.0",
        "In the pinned v0.18.0",
        "Pending PR #87",
        "@v0.18.0)",
    )
    skip_parts = {"changelog", ".git"}
    hits: list[str] = []
    for path in (
        list(REPO_ROOT.glob("*.md"))
        + [REPO_ROOT / "README.md", REPO_ROOT / "landing" / "index.html"]
    ):
        if not path.is_file():
            continue
        if path.name == "CHANGELOG.md":
            continue
        if any(part in skip_parts for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in stale:
            if needle in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert not hits, f"stale dojo pin copy: {hits}"


@pytest.fixture
def release_project():
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


@pytest.mark.parametrize("path,pattern", [
    ("docs/constellation/overview.md", r"\| llm-mailroom\s*\|\s*(v[\d.]+)\s*\|"),
    ("docs/start-here/overview.md", r"\| llm-mailroom\s*\|\s*(v[\d.]+)\s*\|"),
    ("docs/constellation/repos/llm-mailroom.md", r"\| Release\s*\|\s*(v[\d.]+)\s*\|"),
    ("docs/repository-guides/repos/llm-mailroom.md", r"\| Release\s*\|\s*(v[\d.]+)\s*\|"),
    ("docs/the-pipeline-in-depth/running.md", r"`mailroom` ([\d.]+)"),
    ("docs/the-pipeline-in-depth/scoring-and-metrics.md", r"current release \(([\d.]+)\)"),
])
def test_release_docs_match_package_version(release_project, path, pattern):
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    versions = re.findall(pattern, text)
    assert len(versions) == 1, f"{path}: expected one current release declaration"
    assert versions[0].removeprefix("v") == release_project["version"]


@pytest.mark.parametrize("path,pattern", [
    ("docs/constellation/overview.md", r"llm-mailroom pins (v[\d.]+)"),
    ("docs/start-here/overview.md", r"llm-mailroom pins (v[\d.]+)"),
    ("docs/constellation/repos/llm-dojo-scoring.md", r"llm-mailroom pins (v[\d.]+)"),
    ("docs/repository-guides/repos/llm-dojo-scoring.md", r"llm-mailroom pins (v[\d.]+)"),
    ("docs/constellation/architecture.md", r"\| llm-mailroom\s*\| llm-dojo-scoring\s*\| git pin `@(v[\d.]+)`"),
    ("docs/how-it-fits-together/architecture.md", r"\| llm-mailroom\s*\| llm-dojo-scoring\s*\| git pin `@(v[\d.]+)`"),
    ("docs/sister-repos.md", r"`@(v[\d.]+)`"),
    ("docs/pipeline-reference-llm-mailroom/sister-repos.md", r"`@(v[\d.]+)`"),
    ("docs/the-pipeline-in-depth/running.md", r"`@(v[\d.]+)`"),
    ("docs/the-pipeline-in-depth/scoring-and-metrics.md", r"pipeline pins (?:llm-dojo-scoring \*\*)?(v\d+\.\d+\.\d+)"),
])
def test_release_docs_match_declared_dojo_pin(release_project, path, pattern):
    dependency, = [dep for dep in release_project["dependencies"]
                   if dep.startswith("llm-dojo-scoring @")]
    pin = dependency.rsplit("@", 1)[1]
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    pins = re.findall(pattern, text)
    assert pins, f"{path}: missing pipeline dependency pin"
    assert set(pins) == {pin}, f"{path}: documented pins disagree with pyproject.toml"


@pytest.mark.parametrize("path", [
    "docs/the-pipeline-in-depth/running.md",
    "docs/the-pipeline-in-depth/scoring-and-metrics.md",
])
def test_release_operator_docs_do_not_describe_scoring_as_pending(path):
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert not re.search(r"(?:pending|draft)\s+(?:\[)?PR #87", text, re.IGNORECASE)
    assert "until it merges" not in text.lower()
    assert "once it merges" not in text.lower()


def test_release_changelog_rollover_has_unique_ordered_entries(release_project):
    from scripts.sync_gitbook_changelog import parse_changelog

    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = parse_changelog(text, unreleased_date="2026-10-07")
    titles = [release.title for release in releases]
    assert titles[:3] == ["Unreleased", f"v{release_project['version']}", "v0.7.1"]
    assert len(titles) == len(set(titles)), "duplicate changelog release headings"
    current = releases[1]
    assert current.date == "2026-10-07"
    assert current.tags == ("feature", "improvement", "fix")
    assert current.page_rel == "2026/v0-8-0.md"
    # Allow future Unreleased work, but don't duplicate notes already shipped.
    released_notes = set(re.findall(r"^- .+$", current.body, re.MULTILINE))
    unreleased_notes = set(re.findall(r"^- .+$", releases[0].body, re.MULTILINE))
    assert released_notes.isdisjoint(unreleased_notes)
    assert re.findall(r"^### (.+)$", current.body, re.MULTILINE) == ["Added", "Changed", "Fixed"]


@pytest.mark.parametrize("label,comparison", [
    ("Unreleased", "v0.8.0...HEAD"),
    ("v0.8.0", "v0.7.1...v0.8.0"),
    ("v0.7.1", "v0.7.0...v0.7.1"),
    ("v0.7.0", "v0.6.0...v0.7.0"),
])
def test_release_changelog_comparison_links(label, comparison):
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    links = re.findall(rf"^\[{re.escape(label)}\]:\s+(\S+)$", text, re.MULTILINE)
    assert links == [f"https://github.com/Exios66/llm-mailroom/compare/{comparison}"]


def test_release_gitbook_page_and_feed_preserve_all_notes():
    from scripts.sync_gitbook_changelog import parse_changelog

    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release = next(r for r in parse_changelog(text, unreleased_date="2026-10-07")
                   if r.title == "v0.8.0")
    page = (_DOCS / "changelog" / release.page_rel).read_text(encoding="utf-8")
    _, frontmatter, content = page.split("---", 2)
    assert yaml.safe_load(frontmatter)["tags"] == ["feature", "improvement", "fix"]
    assert content.count("# v0.8.0\n") == 1
    assert "Released 2026-10-07." in content
    assert content.split("### Added", 1)[1].strip() == release.body.split("### Added", 1)[1].strip()

    feed = (_DOCS / "changelog" / "README.md").read_text(encoding="utf-8")
    cards = re.findall(r"{% update ([^%]+)%}\n(.*?){% endupdate %}", feed, re.DOTALL)
    titles = [re.search(r"^## (.+)$", body, re.MULTILINE).group(1) for _, body in cards]
    assert titles[:3] == ["Unreleased", "v0.8.0", "v0.7.1"]
    assert titles.count("v0.8.0") == 1
    attrs, body = cards[1]
    assert 'date="2026-10-07"' in attrs
    assert 'tags="feature,improvement,fix"' in attrs
    assert release.body in body
    assert '<a href="2026/v0-8-0.md" class="button primary">' in body
    released_notes = set(re.findall(r"^- .+$", release.body, re.MULTILINE))
    unreleased_notes = set(re.findall(r"^- .+$", cards[0][1], re.MULTILINE))
    assert released_notes.isdisjoint(unreleased_notes)

    listed = _summary_targets(_DOCS / "changelog" / "SUMMARY.md")
    assert listed[:4] == ["README.md", "unreleased.md", "2026/v0-8-0.md", "2026/v0-7-1.md"]
    assert listed.count("2026/v0-8-0.md") == 1
