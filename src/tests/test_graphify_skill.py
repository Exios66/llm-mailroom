"""KANBAN-065: vendored Graphify skill integrity checks.

The ``.opencode/skills/graphify/`` directory is a verbatim vendor of
Graphify-Labs/graphify's opencode agent skill (see PROVENANCE.md). These
network-free tests pin its structure and keep the sibling copy in
``llm-entity-extraction`` byte-identical.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".opencode" / "skills" / "graphify"

REFERENCE_FILES = (
    "add-watch.md",
    "exports.md",
    "extraction-spec.md",
    "github-and-merge.md",
    "hooks.md",
    "query.md",
    "transcribe.md",
    "update.md",
)

SIBLING_SKILL_DIR = (
    REPO_ROOT.parent / "llm-entity-extraction" / ".opencode" / "skills" / "graphify"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_md_exists_with_valid_frontmatter() -> None:
    skill = SKILL_DIR / "SKILL.md"
    assert skill.is_file(), f"missing vendored skill: {skill}"
    text = _read(skill)
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    assert "name: graphify" in text.split("---", 2)[1]


def test_all_reference_sidecars_present() -> None:
    refs = SKILL_DIR / "references"
    for name in REFERENCE_FILES:
        path = refs / name
        assert path.is_file(), f"missing reference sidecar: {path}"
        assert _read(path).strip(), f"reference sidecar is empty: {path}"


def test_provenance_names_upstream_source() -> None:
    provenance = _read(SKILL_DIR / "PROVENANCE.md")
    assert "Graphify-Labs/graphify" in provenance
    assert "b2cd36267456c166788c95be6e68574064a92a42" in provenance


def test_skill_documents_core_workflows() -> None:
    text = _read(SKILL_DIR / "SKILL.md")
    for marker in ("/graphify query", "/graphify path", "/graphify explain", "--update"):
        assert marker in text, f"SKILL.md lost workflow doc: {marker}"


def test_sibling_entity_copy_is_identical() -> None:
    if not SIBLING_SKILL_DIR.is_dir():
        pytest.skip("llm-entity-extraction checkout not present next to this repo")
    for relpath in ("SKILL.md", "PROVENANCE.md", *(
        f"references/{name}" for name in REFERENCE_FILES
    )):
        local = SKILL_DIR / relpath
        sibling = SIBLING_SKILL_DIR / relpath
        assert sibling.is_file(), f"sibling entity missing: {sibling}"
        assert _read(local) == _read(sibling), (
            "vendored graphify skill diverged between repos: "
            f"{relpath} differs (keep both copies byte-identical)"
        )
