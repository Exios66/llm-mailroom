#!/usr/bin/env python3
"""Bump (or check) the llm-dojo-scoring git pin across the repo.

Resolves the latest published GitHub Release on Exios66/llm-dojo-scoring
(or an explicit ``--tag``) and rewrites the pin in ``pyproject.toml`` plus
the documented pin references. Used by humans and by
``.github/workflows/bump-dojo-scoring.yml``.

RELEASE-TIME ONLY: in the mailroom-hub monorepo, development resolves
``llm-dojo-scoring`` from the workspace (``[tool.uv.sources]`` redirect) —
the pin here matters only when cutting a release / building deploy images.
The companion auto-bump workflow is inert inside the monorepo (nested
``.github/`` is not read by GitHub); run this script manually at release
time.

Examples::

    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --check
    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply
    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.12.2
    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOJO_REPO = "Exios66/llm-dojo-scoring"
PIN_RE = re.compile(
    r"(llm-dojo-scoring\s*@\s*git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)"
    r"(v?\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)"
)
# Standalone version tokens that document the pin (avoid bare 0.12.1 in prose
# about honesty gaps — only rewrite explicit pin markers).
DOC_PIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(@git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)(v?\d+\.\d+\.\d+)"),
        r"\g<1>{tag}",
    ),
    (
        re.compile(r"(llm-dojo-scoring @ )(v?\d+\.\d+\.\d+)"),
        r"\g<1>{tag}",
    ),
    (
        # tool-router table: `llm-dojo-scoring @v0.12.1`
        re.compile(r"(llm-dojo-scoring @)(v?\d+\.\d+\.\d+)"),
        r"\g<1>{tag}",
    ),
    (
        re.compile(r"(pinned `@)(v?\d+\.\d+\.\d+)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        re.compile(r"(pinned in `pyproject\.toml` \(`@)(v?\d+\.\d+\.\d+)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        re.compile(r"(dependency `@)(v?\d+\.\d+\.\d+)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        re.compile(r"(pinned dependency @)(v?\d+\.\d+\.\d+)"),
        r"\g<1>{tag}",
    ),
    (
        re.compile(r"(Pin: `llm-dojo-scoring @ git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)(v?\d+\.\d+\.\d+)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        re.compile(r"(llm-dojo-scoring pin and mailroom scoring suites \()(v?\d+\.\d+\.\d+)(\))"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        re.compile(r"(description: llm-dojo-scoring pin and mailroom scoring suites \()(v?\d+\.\d+\.\d+)(\))"),
        r"\g<1>{tag}\g<3>",
    ),
]

PIN_FILES = (
    "pyproject.toml",
    "README.md",
    "docs/sister-repos.md",
    "docs/wiki/Home.md",
    "src/observability/README.md",
    ".cursor/skills/dojo-scoring/SKILL.md",
    ".cursor/skills/mailroom-tool-router/SKILL.md",
    "src/tests/test_dojo_v012.py",
)


def _normalize_tag(tag: str) -> str:
    tag = str(tag or "").strip()
    if not tag:
        raise ValueError("empty tag")
    if not tag.startswith("v"):
        tag = f"v{tag}"
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", tag):
        raise ValueError(f"refusing non-semver dojo tag: {tag!r}")
    return tag


def _bare(tag: str) -> str:
    return _normalize_tag(tag).lstrip("v")


def current_pin(root: Path = REPO_ROOT) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = PIN_RE.search(text)
    if not match:
        raise RuntimeError("llm-dojo-scoring pin not found in pyproject.toml")
    return _normalize_tag(match.group(2))


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "llm-mailroom-bump-dojo-scoring",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def latest_release_tag(repo: str = DOJO_REPO) -> str:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub releases/latest failed: {exc.code} {exc.reason}") from exc
    tag = payload.get("tag_name")
    if not tag:
        raise RuntimeError("releases/latest returned no tag_name")
    return _normalize_tag(tag)


def release_exists(tag: str, repo: str = DOJO_REPO) -> bool:
    tag = _normalize_tag(tag)
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"GitHub release lookup failed: {exc.code} {exc.reason}") from exc


def _rewrite_text(text: str, tag: str) -> str:
    tag = _normalize_tag(tag)
    bare = _bare(tag)
    out = PIN_RE.sub(rf"\g<1>{tag}", text)
    # test module docstring + version assertion
    out = re.sub(
        r'(llm-dojo-scoring v)\d+\.\d+\.\d+( pin)',
        rf"\g<1>{bare}\g<2>",
        out,
    )
    out = re.sub(
        r'(assert llm_dojo_scoring\.__version__ == ")(\d+\.\d+\.\d+)(")',
        rf"\g<1>{bare}\g<3>",
        out,
    )
    out = re.sub(
        r'(def test_installed_dojo_is_v)\d+',
        f"def test_installed_dojo_is_v{bare.replace('.', '')}",
        out,
        count=1,
    )
    out = re.sub(
        r"(`llm-dojo-scoring` \(v)\d+\.\d+\.\d+(\)\.)",
        rf"\g<1>{bare}\g<2>",
        out,
    )
    # observability README link form: [`llm-dojo-scoring`](url) (v0.12.1).
    out = re.sub(
        r"(\[`llm-dojo-scoring`\]\([^)]+\) \(v)\d+\.\d+\.\d+(\)\.)",
        rf"\g<1>{bare}\g<2>",
        out,
    )
    # wiki Home: (the pinned scoring engine, `@v0.12.1`)
    out = re.sub(
        r"(the pinned scoring engine, `)(@?v?\d+\.\d+\.\d+)(`)",
        rf"\g<1>{tag}\g<3>",
        out,
    )
    out = re.sub(
        r"(Pinned as a git dependency \(`@)(v?\d+\.\d+\.\d+)(`)",
        rf"\g<1>{tag}\g<3>",
        out,
    )
    for pattern, repl in DOC_PIN_PATTERNS:
        out = pattern.sub(repl.format(tag=tag, tag_bare=bare), out)
    # skill body "v0.12.1 is additive" style
    out = re.sub(
        r"\bv\d+\.\d+\.\d+ is additive on 0\.\d+\.\d+ formulas",
        f"{tag} is additive on prior formulas",
        out,
    )
    return out


def apply_pin(tag: str, *, root: Path = REPO_ROOT, dry_run: bool = False) -> list[Path]:
    tag = _normalize_tag(tag)
    changed: list[Path] = []
    for rel in PIN_FILES:
        path = root / rel
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8")
        after = _rewrite_text(before, tag)
        if after == before:
            continue
        changed.append(path)
        if not dry_run:
            path.write_text(after, encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if current pin equals desired tag; else exit 2 and print both",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="rewrite pin files to the desired tag",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="explicit dojo release tag (default: GitHub releases/latest)",
    )
    ap.add_argument("--dry-run", action="store_true", help="with --apply, print paths only")
    ap.add_argument(
        "--allow-missing-release",
        action="store_true",
        help="skip verifying the tag exists on GitHub (offline / pre-publish)",
    )
    args = ap.parse_args(argv)

    current = current_pin()
    desired = _normalize_tag(args.tag) if args.tag else latest_release_tag()
    if not args.allow_missing_release and not release_exists(desired):
        print(f"error: no GitHub release for {desired} on {DOJO_REPO}", file=sys.stderr)
        return 1

    print(f"current={current}")
    print(f"desired={desired}")

    if args.check:
        if current == desired:
            print("up_to_date=true")
            return 0
        print("up_to_date=false")
        return 2

    changed = apply_pin(desired, dry_run=args.dry_run)
    if not changed:
        print("changed=0 (already at desired pin or no matching text)")
        return 0 if current == desired else 1
    for path in changed:
        print(f"updated={path.relative_to(REPO_ROOT)}")
    print(f"changed={len(changed)} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
