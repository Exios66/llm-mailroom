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
