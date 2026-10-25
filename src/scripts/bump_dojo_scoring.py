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

The pin may be a release tag (``v0.21.0``) or, while a fix is unreleased, a
40-hex commit SHA (an INTERIM pin). Everything here treats both as a "ref":
a SHA pin renders as its 7-char short form on badges and links to the commit,
and ``--apply --tag vX.Y.Z`` replaces a SHA pin everywhere it is documented.
The auto-bump never downgrades an interim SHA pin to an older release: when
the current pin is a SHA that the target tag does not contain, ``--check``
reports it up to date and ``--apply`` refuses (see ``tag_contains_commit``).

Examples::

    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --check
    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply
    PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.19.1
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
_TAG = r"v?\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?"
_SHA = r"[0-9a-f]{40}(?![0-9A-Za-z])"
# A dojo "ref": a release tag or a full commit SHA (interim pin).
REF = rf"(?:{_SHA}|{_TAG})"
# Badge labels / alt text show a SHA pin as its 7-char short form.
_LABEL = rf"(?:[0-9a-f]{{7}}(?![0-9A-Za-z.])|{_TAG})"
PIN_RE = re.compile(
    r"(llm-dojo-scoring\s*@\s*git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)"
    rf"({REF})"
)


def _c(pattern: str) -> re.Pattern[str]:
    """Compile a doc-pin pattern, expanding ``__REF__`` / ``__LABEL__``."""
    return re.compile(pattern.replace("__REF__", REF).replace("__LABEL__", _LABEL))


# Standalone version tokens that document the pin (avoid bare 0.12.1 in prose
# about honesty gaps — only rewrite explicit pin markers).
DOC_PIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        _c(r"(@git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        _c(r"(llm-dojo-scoring @ )(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        # tool-router table: `llm-dojo-scoring @v0.12.1`
        _c(r"(llm-dojo-scoring @)(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        _c(r"(pinned `@)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(pinned in `pyproject\.toml` \(`@)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(dependency `@)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(pinned dependency @)(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        _c(r"(Pin: `llm-dojo-scoring @ git\+https://github\.com/Exios66/llm-dojo-scoring\.git@)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(llm-dojo-scoring pin and mailroom scoring suites \()(__REF__)(\))"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(description: llm-dojo-scoring pin and mailroom scoring suites \()(__REF__)(\))"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        # test_dojo_v012 pin assertion: "llm-dojo-scoring.git@v0.18.0"
        _c(r"(llm-dojo-scoring\.git@)(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        # GitBook Overview / repo guide: "llm-mailroom pins v0.18.0"
        _c(r"(llm-mailroom pins )(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        # architecture dependency table mailroom row only
        _c(r"(git pin `@)(__REF__)(`(?: as of \d{4}-\d{2}-\d{2})?, auto-bumped)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        # Source-owned dependency-adoption prose replaced the old auto-bump
        # wording; restrict this matcher to the pipeline's dependency row.
        _c(r"(\| llm-mailroom\s*\| llm-dojo-scoring\s*\| git pin `@)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(pinned release `)(__REF__)(`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(In the pinned )(__REF__)(,)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        _c(r"(Dojo pin, \*\*)(__REF__)(\*\*)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        # shields.io dojo pin badge; preserve the trailing color segment
        _c(r"(badge/dojo-)(__LABEL__)(?=-)"),
        r"\g<1>{label}",
    ),
    (
        # Badge destinations must follow the scoring pin, not other releases:
        # a tag links to its release page, an interim SHA pin to its commit.
        _c(
            r"(https://github\.com/Exios66/llm-dojo-scoring/)"
            r"(?:releases/tag|commit)/(__REF__)"
        ),
        r"\g<1>{release_path}",
    ),
    (
        # scoring-and-metrics: "pinned at **v0.21.0** in `pyproject.toml`"
        _c(r"(pinned at \*\*)(__REF__)(\*\* in `pyproject\.toml`)"),
        r"\g<1>{tag}\g<3>",
    ),
    (
        # scoring-and-metrics: "the pipeline pins llm-dojo-scoring **v0.21.0**"
        _c(r"(pipeline pins (?:llm-dojo-scoring \*\*)?)(__REF__)"),
        r"\g<1>{tag}",
    ),
    (
        # scoring-and-metrics reference link text: "`field_scoring.py` (v0.21.0)"
        _c(r"(llm-dojo-scoring `field_scoring\.py` \()(__LABEL__)(\))"),
        r"\g<1>{label}\g<3>",
    ),
    (
        # Source links into the pinned library: blob/<ref>/llm_dojo_scoring/...
        _c(r"(https://github\.com/Exios66/llm-dojo-scoring/blob/)(__REF__)(/)"),
        r"\g<1>{tag}\g<3>",
    ),
]

PIN_FILES = (
    "pyproject.toml",
    "README.md",
    "docs/README.md",
    "docs/sister-repos.md",
    "docs/wiki/Home.md",
    "docs/start-here/getting-started.md",
    "docs/start-here/overview.md",
    "docs/constellation/getting-started.md",
    "docs/how-it-fits-together/architecture.md",
    "docs/repository-guides/repos/llm-dojo-scoring.md",
    "docs/pipeline-reference-llm-mailroom/sister-repos.md",
    "docs/constellation/overview.md",
    "docs/constellation/architecture.md",
    "docs/constellation/repos/llm-dojo-scoring.md",
    "src/observability/README.md",
    ".cursor/skills/dojo-scoring/SKILL.md",
    ".cursor/skills/mailroom-tool-router/SKILL.md",
    "src/tests/test_dojo_v012.py",
    "docs/the-pipeline-in-depth/scoring-and-metrics.md",
    "docs/the-pipeline-in-depth/running.md",
    "docs/pipeline-reference-llm-mailroom/architecture.md",
    "docs/architecture.md",
    "landing/index.html",
)


_SHA_RE = re.compile(r"[0-9a-f]{40}")
_TAG_RE = re.compile(r"v\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?")


def is_sha(ref: str) -> bool:
    """True for exactly 40 lowercase hex characters (an interim commit pin)."""
    return bool(_SHA_RE.fullmatch(str(ref or "")))


def _normalize_tag(tag: str) -> str:
    """Strip whitespace and return a dojo tag or lowercase full commit SHA.

    Add a missing ``v`` prefix to version tags. Accept uppercase SHA input,
    but reject abbreviated SHAs. Raise ValueError for an empty ref or one
    that matches neither the accepted version format nor a 40-hex SHA.
    """
    tag = str(tag or "").strip()
    if not tag:
        raise ValueError("empty tag")
    if is_sha(tag.lower()):
        return tag.lower()
    if not tag.startswith("v"):
        tag = f"v{tag}"
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"refusing non-semver dojo tag: {tag!r}")
    return tag


def _bare(tag: str) -> str:
    """Return the normalized pin without a leading version prefix."""
    return _normalize_tag(tag).lstrip("v")


def pin_label(ref: str) -> str:
    """Badge / alt-text label for a pin: the tag, or a SHA's 7-char form.

    Normalize ``ref`` and raise ValueError if it is not a valid tag or full SHA.
    """
    ref = _normalize_tag(ref)
    return ref[:7] if is_sha(ref) else ref


def pin_release_path(ref: str) -> str:
    """GitHub path a pin links to: its release page, or its commit for a SHA.

    Normalize ``ref`` and raise ValueError if it is not a valid tag or full SHA.
    """
    ref = _normalize_tag(ref)
    return f"commit/{ref}" if is_sha(ref) else f"releases/tag/{ref}"


def current_pin(root: Path = REPO_ROOT) -> str:
    """Read and normalize the scoring dependency pin from pyproject.toml."""
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
    """Check for a published GitHub release, raising on errors other than 404."""
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


def tag_contains_commit(sha: str, tag: str, repo: str = DOJO_REPO) -> bool:
    """True when ``tag`` is at or after commit ``sha`` (GitHub compare API).

    Guards the auto-bump against DOWNGRADING an interim SHA pin to an older
    release: ``compare/<sha>...<tag>`` is ``ahead`` or ``identical`` only when
    the tag contains the pinned commit.

    ``repo`` is the GitHub ``owner/name``. Return False for other comparison
    statuses or a missing status. Invalid refs raise ValueError; HTTP errors
    become RuntimeError. Other network and response-decoding errors propagate.
    """
    sha, tag = _normalize_tag(sha), _normalize_tag(tag)
    url = f"https://api.github.com/repos/{repo}/compare/{sha}...{tag}"
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub compare failed: {exc.code} {exc.reason}") from exc
    return payload.get("status") in ("ahead", "identical")


def _rewrite_text(text: str, tag: str) -> str:
    """Return text with recognized dojo pins, badges, and test references updated.

    ``tag`` accepts a version tag or full commit SHA. Strip surrounding
    whitespace, add a missing ``v`` prefix to tags, and lowercase SHAs;
    raise ValueError for an empty or invalid ref. SHA pins use seven-character
    badge labels and commit links, leaving version assertions and test names
    unchanged; release tags also update those version references.
    Text without recognized patterns is returned unchanged. No files are written.
    """
    tag = _normalize_tag(tag)
    out = PIN_RE.sub(rf"\g<1>{tag}", text)
    if not is_sha(tag):
        # Version-number rewrites (docstring, __version__ assertion, test name)
        # only make sense for a release; a SHA pin has no version number.
        bare = _bare(tag)
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
    label = pin_label(tag)
    release_path = pin_release_path(tag)
    # observability README link form: [`llm-dojo-scoring`](url) (v0.12.1).
    out = _c(r"(\[`llm-dojo-scoring`\]\([^)]+\) \()(__LABEL__)(\)\.)").sub(
        rf"\g<1>{label}\g<3>", out
    )
    out = _c(r"(`llm-dojo-scoring` \()(__LABEL__)(\)\.)").sub(
        rf"\g<1>{label}\g<3>", out
    )
    # wiki Home: (the pinned scoring engine, `@v0.12.1`)
    out = _c(r"(the pinned scoring engine, `)(@?__REF__)(`)").sub(
        rf"\g<1>{tag}\g<3>", out
    )
    # landing / README badge alt: llm-dojo-scoring v0.19.1
    out = _c(r"(alt=\"llm-dojo-scoring )(__LABEL__)(\")").sub(
        rf"\g<1>{label}\g<3>", out
    )
    out = _c(r"([Pp]inned as a git dependency \(`@)(__REF__)(`)").sub(
        rf"\g<1>{tag}\g<3>", out
    )
    # Documented invocation of this script in docs/sister-repos.md.
    out = _c(r"(bump_dojo_scoring\.py --apply --tag )(__REF__)").sub(
        rf"\g<1>{tag}", out
    )
    for pattern, repl in DOC_PIN_PATTERNS:
        out = pattern.sub(
            repl.format(tag=tag, label=label, release_path=release_path), out
        )
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
    """Check or apply a scoring pin using CLI arguments (default: sys.argv).

    Print the current and desired refs and check results or updated paths.
    ``--check`` returns 0 for a matching pin or an interim SHA not contained
    in the target release, and 2 when an update is needed. ``--apply`` writes
    recognized pin files unless ``--dry-run`` is set. Return 1 for a missing
    release, a refused SHA downgrade, or no changes when the pins differ;
    otherwise return 0. ``--allow-missing-release`` skips the release-existence check
    and the SHA ancestry guard.

    Argument parsing may raise SystemExit. Invalid refs raise ValueError;
    file I/O and GitHub lookup errors propagate.
    """
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

    if is_sha(current) and current != desired and not args.allow_missing_release:
        if not tag_contains_commit(current, desired):
            # Interim SHA pin that the target release does not contain: moving
            # to it would downgrade the pin (e.g. the daily backstop resolving
            # releases/latest to a tag older than the unreleased fix).
            print(f"pin_ahead_of_release=true ({desired} does not contain {current})")
            if args.check:
                print("up_to_date=true")
                return 0
            print("error: refusing to downgrade an interim SHA pin", file=sys.stderr)
            return 1

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
