"""hub#53 — docs-truth guard: every `docs/*.md` path referenced in tracked
prose must exist (or be explicitly annotated as pruned/upstream). Cheap
network-free net so a doc rewrite cannot silently dangle a new reference."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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
            text = path.read_text(encoding="utf-8", errors="ignore")
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
        # docs/reports/* is deliberately pruned (see docs/reports/README.md)
        if ref.startswith("docs/reports/") and ref != "docs/reports/README.md":
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
        if "v7-taxonomy.md" in path.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"v7-taxonomy.md still referenced: {hits}"