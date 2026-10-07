"""hub#53 — docs-truth guard: every `docs/*.md` path referenced in tracked
prose must exist (or be explicitly annotated as pruned/upstream). Cheap
network-free net so a doc rewrite cannot silently dangle a new reference."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

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
_SKIP_SUMMARY_DIRS = {
    "wiki",
    "assets",
    "changelog",
    ".gitbook",
    # Canonical copies GitBook re-exported under URL-mapped folders.
    "constellation",
}
_SKIP_SUMMARY_FILES = {"SUMMARY.md"}
# GitBook Git Sync rewrote the Mailroom Docs TOC into nested folders. The
# leftover GitHub-canonical copies (docs/*.md, docs/constellation/) stay in
# the repo but are not the published space tree. Changelog is a second space.
_GITBOOK_DOCS_DIRS = {
    "start-here",
    "how-it-fits-together",
    "mailroom-dataset",
    "repository-guides",
    "pipeline-reference-llm-mailroom",
    "the-pipeline-in-depth",
    "about-this-site",
}


def _summary_targets(summary_path: Path) -> list[str]:
    text = summary_path.read_text(encoding="utf-8")
    return _SUMMARY_LINK.findall(text)


def test_gitbook_summary_lists_every_publishable_page():
    """GitBook only publishes pages listed in SUMMARY.md (maintaining.md)."""
    listed = _summary_targets(_DOCS / "SUMMARY.md")
    missing_files = [rel for rel in listed if not (_DOCS / rel).is_file()]
    assert not missing_files, f"SUMMARY.md points at missing files: {missing_files}"

    unpublished: list[str] = []
    for path in _DOCS.rglob("*.md"):
        rel = path.relative_to(_DOCS).as_posix()
        parts = path.relative_to(_DOCS).parts
        if any(part in _SKIP_SUMMARY_DIRS for part in parts):
            continue
        if path.name in _SKIP_SUMMARY_FILES:
            continue
        if parts[0] not in _GITBOOK_DOCS_DIRS and rel != "README.md":
            continue
        if rel not in listed:
            unpublished.append(rel)
    assert not unpublished, (
        f"docs pages not in SUMMARY.md (GitBook will not publish them): {unpublished}"
    )


def test_gitbook_toc_nests_docker_modal_and_sandbox_reports():
    summary = (_DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    assert "* [Deployment](pipeline-reference-llm-mailroom/deployment/README.md)" in summary
    assert "  * [Docker](pipeline-reference-llm-mailroom/deployment/docker-deployment.md)" in summary
    assert "  * [Modal + vLLM](pipeline-reference-llm-mailroom/deployment/modal-vllm.md)" in summary
    assert "* [local-mailroom-sandbox](repository-guides/repos/local-mailroom-sandbox/README.md)" in summary
    assert "    * [Documentation](repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-docs.md)" in summary
    assert "    * [Run reports](repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-reports.md)" in summary
    assert "    * [Visuals](repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-visuals.md)" in summary
    assert "* [Data and corpora](how-it-fits-together/data-and-corpora.md)" in summary
    assert "* [Mailroom-Corpus-EDA](repository-guides/repos/mailroom-corpus-eda.md)" in summary

    docker = (
        _DOCS / "pipeline-reference-llm-mailroom" / "deployment" / "docker-deployment.md"
    ).read_text(encoding="utf-8")
    modal = (
        _DOCS / "pipeline-reference-llm-mailroom" / "deployment" / "modal-vllm.md"
    ).read_text(encoding="utf-8")
    reports = (
        _DOCS
        / "repository-guides"
        / "repos"
        / "local-mailroom-sandbox"
        / "local-mailroom-sandbox-reports.md"
    ).read_text(encoding="utf-8")
    visuals = (
        _DOCS
        / "repository-guides"
        / "repos"
        / "local-mailroom-sandbox"
        / "local-mailroom-sandbox-visuals.md"
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

    data = (_DOCS / "how-it-fits-together" / "data-and-corpora.md").read_text(
        encoding="utf-8"
    )
    overview = (_DOCS / "mailroom-dataset" / "README.md").read_text(encoding="utf-8")
    visuals = (_DOCS / "mailroom-dataset" / "visualizations.md").read_text(
        encoding="utf-8"
    )
    pages = "https://exios66.github.io/Mailroom-Corpus-EDA"
    assert "mailroom-dataset/" in data
    assert "Lucius-Morningstar/mailroom-dataset" in data
    assert f'<iframe src="{pages}/"' in overview
    assert "huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset/embed/viewer" in overview
    assert '{% embed url="https://exios66.github.io/Mailroom-Corpus-EDA/" %}' in overview
    assert f'<iframe src="{pages}/"' in visuals
    assert f"{pages}/figures_interactive/04_text_length_violin.html" in visuals
    assert f"{pages}/figures_interactive/23_imbalance_treemap.html" in visuals
    assert visuals.count(f"{pages}/figures_interactive/") >= 18
    for n, name in (
        ("01", "type_and_subclass_distribution"),
        ("08", "cuad_clause_presence"),
        ("14", "maud_answer_distribution"),
        ("21", "corr_intent"),
        ("30", "metadata_cardinality"),
    ):
        assert f"{n}_{name}.png" in visuals


def test_gitbook_toc_nests_mailroom_dataset_section():
    summary = (_DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    assert "## Mailroom dataset" in summary
    assert "* [Overview](mailroom-dataset/README.md)" in summary
    assert "* [Classes and strata](mailroom-dataset/classes-and-strata.md)" in summary
    assert "* [Configs](mailroom-dataset/configs.md)" in summary
    assert "* [Source corpora](mailroom-dataset/source-corpora.md)" in summary
    assert "  * [CUAD contracts](mailroom-dataset/sources/cuad-contracts.md)" in summary
    assert "  * [MAUD merger agreements](mailroom-dataset/sources/maud-merger-agreements.md)" in summary
    assert "  * [SEC corporate records](mailroom-dataset/sources/edgar-corporate-records.md)" in summary
    assert "  * [Enron correspondence](mailroom-dataset/sources/enron-correspondence.md)" in summary
    assert "  * [CMS insurance claims](mailroom-dataset/sources/cms-insurance-claims.md)" in summary
    assert "* [EDA reports](mailroom-dataset/eda-reports.md)" in summary
    assert "* [Visualizations](mailroom-dataset/visualizations.md)" in summary

    overview = (_DOCS / "mailroom-dataset" / "README.md").read_text(encoding="utf-8")
    strata = (_DOCS / "mailroom-dataset" / "classes-and-strata.md").read_text(encoding="utf-8")
    eda = (_DOCS / "mailroom-dataset" / "eda-reports.md").read_text(encoding="utf-8")
    visuals = (_DOCS / "mailroom-dataset" / "visualizations.md").read_text(
        encoding="utf-8"
    )
    assert "Lucius-Morningstar/mailroom-dataset" in overview
    assert "3,302" in overview
    assert "ed7576b6" in overview
    assert "55" in strata
    assert "mixed_cash_stock_election" in strata
    assert "run_all.py" in eda
    assert "P0" in eda and "P6" in eda
    assert "13,753" in eda
    png_base = "raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures"
    assert png_base in visuals
    for stem in (
        "01_type_and_subclass_distribution.png",
        "08_cuad_clause_presence.png",
        "13_maud_task_frequency.png",
        "16_claim_amount_distribution.png",
        "21_corr_intent.png",
        "23_imbalance_treemap.png",
        "30_metadata_cardinality.png",
    ):
        assert stem in visuals
    assert "figures_interactive" in visuals
    assert "exios66.github.io/Mailroom-Corpus-EDA" in visuals
    assert '<iframe src="https://exios66.github.io/Mailroom-Corpus-EDA/"' in visuals
    assert "huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset/embed/viewer" in overview


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


def test_gitbook_changelog_space_mirrors_repo_changelog():
    """The Changelog site section is a generated mirror of CHANGELOG.md."""
    script = REPO_ROOT / "src" / "scripts" / "sync_gitbook_changelog.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    changelog = REPO_ROOT / "CHANGELOG.md"
    changelog_text = changelog.read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", changelog_text, re.MULTILINE)
    assert "Unreleased" in headings
    assert "v0.8.0" in headings
    assert "v0.7.1" in headings
    assert "v0.7.0" in headings

    space = _DOCS / "changelog"
    feed = (space / "README.md").read_text(encoding="utf-8")
    summary = (space / "SUMMARY.md").read_text(encoding="utf-8")
    unreleased = (space / "unreleased.md").read_text(encoding="utf-8")
    v080 = (space / "2026" / "v0-8-0.md").read_text(encoding="utf-8")
    v071 = (space / "2026" / "v0-7-1.md").read_text(encoding="utf-8")

    assert "{% updates format=\"full\" %}" in feed
    assert "## Unreleased" in feed
    assert "## v0.8.0" in feed
    assert "## v0.7.1" in feed
    assert "* [Unreleased](unreleased.md)" in summary
    assert "* [v0.8.0](2026/v0-8-0.md)" in summary
    assert "* [v0.7.1](2026/v0-7-1.md)" in summary
    assert "Unreleased work on `main`" in unreleased
    assert "Mode G — LiteLLM gateway + Modal GPU tiers" in v080
    assert "GitBook Changelog space" in v080
    assert "Corpus revision re-pinned to the GT-closure tip" in v071
    assert "Feature description" not in feed
    assert "Product improvement" not in feed
    assert "gitbookio.github.io/onboarding-template-images" not in feed

    listed = _summary_targets(space / "SUMMARY.md")
    unpublished: list[str] = []
    for path in space.rglob("*.md"):
        rel = path.relative_to(space).as_posix()
        if path.name == "SUMMARY.md":
            continue
        if rel not in listed:
            unpublished.append(rel)
    assert not unpublished, f"changelog pages not in changelog/SUMMARY.md: {unpublished}"
    missing = [rel for rel in listed if not (space / rel).is_file()]
    assert not missing, f"changelog/SUMMARY.md points at missing files: {missing}"

    site_cfg = yaml.safe_load((_DOCS / "gitbook-docs.yaml").read_text(encoding="utf-8"))

    def _walk(nodes):
        for node in nodes:
            yield node
            yield from _walk(node.get("children") or [])

    nodes = list(_walk(site_cfg["site"]["structure"]))
    changelog_space = next(node for node in nodes if node.get("key") == "space-1")
    assert changelog_space["title"] == "Changelog"
    assert changelog_space["content"]["directory"] == "./changelog"
    assert changelog_space.get("draft") not in (True, "true")
    tags = (space / ".gitbook" / "tags.yaml").read_text(encoding="utf-8")
    assert "tag: feature" in tags
    assert "tag: improvement" in tags
    assert "tag: fix" in tags
