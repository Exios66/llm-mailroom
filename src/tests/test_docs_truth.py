"""hub#53 — docs-truth guard: every `docs/*.md` path referenced in tracked
prose must exist (or be explicitly annotated as pruned/upstream). Cheap
network-free net so a doc rewrite cannot silently dangle a new reference."""

from __future__ import annotations

import re
from pathlib import Path

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
_SKIP_SUMMARY_DIRS = {"wiki", "assets"}
_SKIP_SUMMARY_FILES = {"SUMMARY.md"}


def _summary_targets() -> list[str]:
    text = (_DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    return _SUMMARY_LINK.findall(text)


def test_gitbook_summary_lists_every_publishable_page():
    """GitBook only publishes pages listed in SUMMARY.md (maintaining.md)."""
    listed = _summary_targets()
    missing_files = [rel for rel in listed if not (_DOCS / rel).is_file()]
    assert not missing_files, f"SUMMARY.md points at missing files: {missing_files}"

    unpublished: list[str] = []
    for path in _DOCS.rglob("*.md"):
        rel = path.relative_to(_DOCS).as_posix()
        if any(part in _SKIP_SUMMARY_DIRS for part in path.relative_to(_DOCS).parts):
            continue
        if path.name in _SKIP_SUMMARY_FILES:
            continue
        if rel not in listed:
            unpublished.append(rel)
    assert not unpublished, (
        f"docs pages not in SUMMARY.md (GitBook will not publish them): {unpublished}"
    )


def test_gitbook_toc_nests_docker_modal_and_sandbox_reports():
    summary = (_DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    assert "* [Deployment](deployment.md)" in summary
    assert "  * [Docker](docker-deployment.md)" in summary
    assert "  * [Modal + vLLM](modal-vllm.md)" in summary
    assert "* [local-mailroom-sandbox](constellation/repos/local-mailroom-sandbox.md)" in summary
    assert "    * [Documentation](constellation/repos/local-mailroom-sandbox-docs.md)" in summary
    assert "    * [Run reports](constellation/repos/local-mailroom-sandbox-reports.md)" in summary
    assert "    * [Visuals](constellation/repos/local-mailroom-sandbox-visuals.md)" in summary

    docker = (_DOCS / "docker-deployment.md").read_text(encoding="utf-8")
    modal = (_DOCS / "modal-vllm.md").read_text(encoding="utf-8")
    reports = (
        _DOCS / "constellation" / "repos" / "local-mailroom-sandbox-reports.md"
    ).read_text(encoding="utf-8")
    visuals = (
        _DOCS / "constellation" / "repos" / "local-mailroom-sandbox-visuals.md"
    ).read_text(encoding="utf-8")
    assert "Mode G" in docker
    assert "docker-compose.full.yml" in docker
    assert "mailroom-vllm" in modal
    assert "SAND-37" in reports
    assert "SAND-032" in reports
    assert "mailroom-reports.html" in reports
    assert "raw.githubusercontent.com/Exios66/local-mailroom-sandbox" in visuals
    assert "sandbox watch" in visuals
    assert "cmp-quality.png" in visuals
    assert "cost-by-specialist-hardware.png" in visuals
    assert "board-terminal.svg" in visuals


def test_docker_and_modal_pages_cover_operator_matrix():
    docker = (_DOCS / "docker-deployment.md").read_text(encoding="utf-8")
    modal = (_DOCS / "modal-vllm.md").read_text(encoding="utf-8")
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
