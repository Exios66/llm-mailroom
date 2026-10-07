"""Project Cursor skills are present and discoverable."""

from __future__ import annotations

import re
from pathlib import Path

REQUIRED = (
    "mailroom-tool-router",
    "openrouter",
    "ollama",
    "modal",
    "langfuse",
    "braintrust",
    "apache-phoenix",
    "huggingface",
    "langgraph",
    "dojo-scoring",
    "legalbench",
)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cursor_skills_exist_with_frontmatter():
    root = REPO_ROOT / ".cursor" / "skills"
    assert root.is_dir()
    readme = root / "README.md"
    assert readme.is_file()
    for name in REQUIRED:
        path = root / name / "SKILL.md"
        assert path.is_file(), f"missing skill {path}"
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        assert match, f"{path} missing YAML frontmatter"
        meta = match.group(1)
        assert f"name: {name}" in meta
        assert "description:" in meta
        assert len(meta.strip()) > 40
        assert name in readme.read_text(encoding="utf-8")


def test_router_points_at_every_specialty_skill():
    router = (REPO_ROOT / ".cursor" / "skills" / "mailroom-tool-router" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for name in REQUIRED:
        if name == "mailroom-tool-router":
            continue
        assert f"../{name}/SKILL.md" in router, f"router missing link to {name}"


_ROUTER = REPO_ROOT / ".cursor" / "skills" / "mailroom-tool-router" / "SKILL.md"


def test_router_relative_skill_links_resolve():
    text = _ROUTER.read_text(encoding="utf-8")
    links = re.findall(r"\]\((\.\./[^)\s]+/SKILL\.md)\)", text)
    assert links, "router has no relative skill links"
    for link in links:
        target = (_ROUTER.parent / link).resolve()
        assert target.is_file(), f"router link does not resolve: {link}"


def test_router_repo_anchor_paths_resolve():
    text = _ROUTER.read_text(encoding="utf-8")
    section = text.split("## Repo anchors", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    paths = []
    for line in section.splitlines():
        if line.startswith("|"):
            paths.extend(re.findall(r"`([^`]+)`", line))
    assert paths, "no backticked paths found in Repo anchors"
    for rel in paths:
        if "*" in rel:
            assert list(REPO_ROOT.glob(rel)), f"anchor glob matches nothing: {rel}"
        else:
            assert (REPO_ROOT / rel).exists(), f"stale anchor path: {rel}"


def test_router_dojo_pin_matches_pyproject():
    router_pin = re.search(r"llm-dojo-scoring @(v\d+(?:\.\d+)*)", _ROUTER.read_text(encoding="utf-8"))
    assert router_pin, "router has no llm-dojo-scoring @vX.Y.Z pin"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    git_pin = re.search(r"llm-dojo-scoring\.git@(v[\d.]+)", pyproject)
    assert git_pin, "pyproject.toml has no llm-dojo-scoring git pin"
    assert router_pin.group(1) == git_pin.group(1)
