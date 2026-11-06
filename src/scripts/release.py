#!/usr/bin/env python3
"""Release governor for llm-mailroom: status, check, cut, notes, publish.

Keeps CHANGELOG.md <-> pyproject.toml <-> the documented "current release"
strings <-> git tag <-> GitHub release in one chain, so a version is never
moved by hand. Mirrors llm-dojo-scoring's ``scripts/release.py`` (same
semantics), minus the Dojo-only wheel assets and consumer pins.

    python3 src/scripts/release.py status [--json]
    python3 src/scripts/release.py check [--json]
    python3 src/scripts/release.py cut VERSION [--apply] [--date YYYY-MM-DD]
                                     [--allow-dirty] [--allow-empty] [--json]
    python3 src/scripts/release.py notes VERSION [--commit REF] [--out FILE] [--json]
    python3 src/scripts/release.py publish VERSION [--apply] [--push]
                                     [--commit REF] [--allow-unmerged] [--json]

Contract (written for agents):
  - every mutating command is a dry run unless ``--apply`` is passed
  - ``--json`` prints a stable object with a ``next`` list of follow-up steps
  - exit 0 = ok / clean dry run, 1 = structural refusal or failed check,
    2 = usage error
  - never force-pushes, never deletes or moves tags, never amends commits

Not to be confused with ``bump_dojo_scoring.py``, which moves the
``llm-dojo-scoring`` *dependency* pin, not this package's own version.

Stdlib + local git only; ``gh`` is needed just for ``publish``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO = "Exios66/llm-mailroom"
REPO_URL = f"https://github.com/{REPO}"
TAG_PREFIX = "v"
SYNC_SCRIPT = "src/scripts/sync_gitbook_changelog.py"
SCRIPT = "python3 src/scripts/release.py"

# Every file that states this package's OWN current version. Each pattern has
# exactly one group (the X.Y.Z); `check` requires every match to equal the
# pyproject version and `cut` rewrites every match. Mentions of other versions
# (the Dojo pin, historical releases) are deliberately not listed.
_V = r"(\d+\.\d+\.\d+)"
_BADGE = rf"badge/release-v{_V}-2EA043"
_TAG_URL = rf"{re.escape(REPO)}/releases/tag/v{_V}"
_OVERVIEW_ROW = rf"^\| llm-mailroom\s*\|\s*v{_V}\s*\|"
_GUIDE_ROW = rf"^\| Release\s*\|\s*v{_V}\s*\|"
VERSION_LOCATIONS: dict[str, list[str]] = {
    "pyproject.toml": [rf'^version\s*=\s*"{_V}"'],
    "README.md": [_BADGE, _TAG_URL, rf"\[`v{_V}`\]\({re.escape(REPO_URL)}/releases/"],
    "landing/index.html": [_BADGE, _TAG_URL, rf'alt="Release v{_V}"', rf"<em>release</em> · v{_V}"],
    "docs/README.md": [_BADGE, _TAG_URL, rf"\*\*release\*\* · v{_V}"],
    "docs/constellation/overview.md": [_OVERVIEW_ROW],
    "docs/start-here/overview.md": [_OVERVIEW_ROW],
    "docs/constellation/repos/llm-mailroom.md": [_GUIDE_ROW],
    "docs/repository-guides/repos/llm-mailroom.md": [_GUIDE_ROW],
    "docs/the-pipeline-in-depth/running.md": [rf"`mailroom` {_V}"],
    "docs/the-pipeline-in-depth/scoring-and-metrics.md": [rf"current release \({_V}\)"],
    "src/tests/test_landing.py": [rf'"release-v{_V}-2EA043"'],
    # The display contract embeds the package version; a cut must move it too
    # or test_contract_export fails right after the bump.
    "contract/mailroom-contract.json": [rf'^  "pipeline_version": "{_V}"'],
}
PYPROJECT_PATTERN = VERSION_LOCATIONS["pyproject.toml"][0]

SECTION_RE = re.compile(r"^## \[(?P<name>[^\]]+)\](?P<stamp> - \d{4}-\d{2}-\d{2})?\s*$")
FOOTER_LINK_RE = re.compile(r"^\[(?P<name>[^\]]+)\]:\s+(?P<url>\S+)\s*$", re.MULTILINE)
UNRELEASED_LINK_RE = re.compile(
    r"^\[Unreleased\]:\s+(?P<base>\S+?)/compare/(?P<prev>v\d+\.\d+\.\d+)\.\.\.HEAD\s*$",
    re.MULTILINE,
)
_NUM = r"(0|[1-9]\d*)"  # SemVer: no leading zeros
SEMVER_RE = re.compile(rf"^v?{_NUM}\.{_NUM}\.{_NUM}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # the only form sync_gitbook_changelog.py parses
PR_MERGE_RE = re.compile(r"\bMerge pull request #(?P<num>\d+) from (?P<branch>\S+)")


class ReleaseError(Exception):
    """Structural refusal (exit 1)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command in the repository root; raise ``ReleaseError`` on failure when ``check``."""
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    except FileNotFoundError as exc:  # e.g. no ``gh`` installed
        raise ReleaseError(f"{' '.join(cmd)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise ReleaseError(f"{' '.join(cmd)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def git(*args: str) -> str:
    """Return the stdout of a git command."""
    return run(["git", *args]).stdout


def emit(payload: dict, *, as_json: bool, human_lines: list[str] | None = None) -> int:
    """Print the payload as JSON or as human lines plus next steps; return its exit code."""
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in human_lines or []:
            print(line)
        if payload.get("next"):
            print("\nnext:")
            for step in payload["next"]:
                print(f"  - {step}")
    return int(payload.get("exit_code", 0))


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` for ``X.Y.Z`` / ``vX.Y.Z``, else ``None``."""
    match = SEMVER_RE.match(version)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def normalize_version(version: str) -> str:
    """Strip the tag prefix and require ``X.Y.Z``."""
    version = version.removeprefix(TAG_PREFIX)
    if parse_semver(version) is None:
        raise ReleaseError(f"{version!r} is not semver X.Y.Z")
    return version


def tag_for(version: str) -> str:
    """Return the git tag name of a version."""
    return f"{TAG_PREFIX}{normalize_version(version)}"


def read(rel: str) -> str:
    """Read a repository file."""
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def pyproject_version(text: str | None = None) -> str:
    """Return the version declared in ``pyproject.toml`` (or in ``text``)."""
    match = re.search(PYPROJECT_PATTERN, read("pyproject.toml") if text is None else text, re.MULTILINE)
    if not match:
        raise ReleaseError("no version = line in pyproject.toml")
    return match[1]


# ---------------------------------------------------------------------------
# changelog / tags / chain
# ---------------------------------------------------------------------------


def parse_changelog(text: str) -> list[dict]:
    """Parse ``## [name]`` sections (name, version, stamp, line_no, body). Unreleased must come first."""
    lines = text.splitlines()
    heads = [(i, m) for i, line in enumerate(lines) if (m := SECTION_RE.match(line))]
    if not heads:
        raise ReleaseError("no ## [section] headers in CHANGELOG.md")
    if heads[0][1]["name"].lower() != "unreleased":
        raise ReleaseError("CHANGELOG.md must open with ## [Unreleased]")
    footer = next(
        (i for i in range(heads[-1][0] + 1, len(lines)) if FOOTER_LINK_RE.match(lines[i])), len(lines)
    )
    sections = []
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else footer
        sections.append(
            {
                "name": m["name"],
                "version": f"{sv[0]}.{sv[1]}.{sv[2]}" if (sv := parse_semver(m["name"])) else None,
                "stamp": (m["stamp"] or "").lstrip(" -"),
                "is_unreleased": n == 0,
                "line_no": i + 1,
                "body": "\n".join(lines[i + 1 : end]).strip(),
            }
        )
    return sections


def changelog_text(ref: str | None = None) -> str:
    """CHANGELOG.md from the working tree, or as committed at ``ref``."""
    return read("CHANGELOG.md") if ref is None else git("show", f"{ref}:CHANGELOG.md")


def list_version_tags() -> list[dict]:
    """Local ``vX.Y.Z`` tags, newest first (type, peeled commit, subject)."""
    out = git(
        "tag", "--list", f"{TAG_PREFIX}*",
        "--format=%(refname:short)%09%(objecttype)%09%(objectname)%09%(*objectname)%09%(contents:subject)",
    )
    tags = []
    for line in out.splitlines():
        name, objtype, obj, peeled, subject = (line.split("\t") + [""] * 5)[:5]
        sv = parse_semver(name)
        if sv:
            tags.append(
                {
                    "tag": name,
                    "version": name.removeprefix(TAG_PREFIX),
                    "type": objtype,
                    "commit": peeled or obj,
                    "subject": subject,
                    "_key": sv,
                }
            )
    tags.sort(key=lambda t: t["_key"], reverse=True)
    for t in tags:
        del t["_key"]
    return tags


def chain_state() -> dict:
    """Snapshot of the release chain: versions, tags, changelog sections, working tree."""
    sections = parse_changelog(changelog_text())
    released = [s["version"] for s in sections if s["version"]]
    tags = list_version_tags()
    return {
        "repo": REPO,
        "head": git("rev-parse", "HEAD").strip(),
        "dirty": bool(git("status", "--porcelain").strip()),
        "version": pyproject_version(),
        "newest_changelog_version": released[0] if released else None,
        "tags": tags,
        "newest_tag": tags[0]["tag"] if tags else None,
        "sections": [s["name"] for s in sections],
        "unreleased_empty": not sections[0]["body"],
    }


def location_errors(version: str) -> list[str]:
    """Version-location drift: every state of the package's own version must equal ``version``."""
    errors = []
    for rel, patterns in VERSION_LOCATIONS.items():
        if not (REPO_ROOT / rel).is_file():
            errors.append(f"{rel}: missing (listed in VERSION_LOCATIONS)")
            continue
        text = read(rel)
        for pattern in patterns:
            found = re.findall(pattern, text, re.MULTILINE)
            if not found:
                errors.append(f"{rel}: no match for /{pattern}/")
            elif set(found) != {version}:
                errors.append(f"{rel}: states {sorted(set(found))}, pyproject says {version}")
    return errors


def run_sync(*, check: bool) -> subprocess.CompletedProcess[str]:
    """Run (or ``--check``) the GitBook changelog mirror."""
    return run([sys.executable, str(REPO_ROOT / SYNC_SCRIPT), *(["--check"] if check else [])], check=False)


def check_chain(state: dict) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for inconsistencies in the release chain."""
    errors: list[str] = []
    warnings: list[str] = []
    version = state["version"]
    current = parse_semver(version)
    if current is None:
        return [f"pyproject version {version!r} is not semver"], warnings
    errors += location_errors(version)

    sections = parse_changelog(changelog_text())
    released = [s for s in sections if s["version"]]
    names = [s["version"] for s in released]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"duplicate CHANGELOG section [{dup}]")
    keys = [parse_semver(n) for n in names]
    if keys != sorted(keys, reverse=True):
        errors.append("CHANGELOG version sections are not in descending order")
    if names and names[0] != version:
        errors.append(f"newest CHANGELOG section is [{names[0]}] but pyproject says {version}")

    links = {m["name"]: m["url"] for m in FOOTER_LINK_RE.finditer(changelog_text())}
    unreleased = UNRELEASED_LINK_RE.search(changelog_text())
    if not unreleased:
        errors.append("CHANGELOG footer has no [Unreleased]: .../compare/vX.Y.Z...HEAD link")
    elif unreleased["prev"] != f"v{version}":
        errors.append(f"[Unreleased] compares from {unreleased['prev']}, expected v{version}")
    if f"v{version}" not in links:
        errors.append(f"CHANGELOG footer has no [v{version}] link")

    tagged = {t["version"]: t for t in state["tags"]}
    for tag in state["tags"]:
        if tag["version"] not in names:
            warnings.append(f"tag {tag['tag']} has no matching CHANGELOG section")
        if tag["type"] != "tag":
            warnings.append(f"tag {tag['tag']} is lightweight — prefer annotated tags")
    if version not in tagged:
        warnings.append(f"tag {tag_for(version)} is not local yet (publish creates it)")
    if state["tags"] and parse_semver(state["tags"][0]["version"]) > current:
        errors.append(f"pyproject {version} is behind newest tag {state['newest_tag']}")

    proc = run_sync(check=True)
    if proc.returncode != 0:
        detail = "; ".join([ln.strip() for ln in proc.stderr.splitlines() if ln.startswith("  ")][:3])
        errors.append(f"docs/changelog/ is stale — run `PYTHONPATH=src python3 {SYNC_SCRIPT}` ({detail})")
    return errors, warnings


# ---------------------------------------------------------------------------
# cut
# ---------------------------------------------------------------------------


def stamp_changelog(text: str, version: str, today: str) -> str:
    """Move ``[Unreleased]`` to ``## [vX.Y.Z] - date``, open a fresh one, and extend the footer links."""
    lines = text.splitlines()
    sections = parse_changelog(text)
    start = sections[0]["line_no"] - 1
    if len(sections) > 1:
        end = sections[1]["line_no"] - 1
    else:  # Unreleased is the only section: its body stops at the footer links
        end = next((i for i in range(start + 1, len(lines)) if FOOTER_LINK_RE.match(lines[i])), len(lines))
    body = sections[0]["body"].splitlines()
    tag = tag_for(version)
    released = [f"## [{tag}] - {today}", "", *([*body, ""] if body else [])]
    stamped = [*lines[:start], "## [Unreleased]", "", *released, *lines[end:]]
    out = "\n".join(stamped).rstrip() + "\n"

    def footer_links(m: re.Match[str]) -> str:
        base, prev = m["base"], m["prev"]
        return f"[Unreleased]: {base}/compare/{tag}...HEAD\n[{tag}]: {base}/compare/{prev}...{tag}"

    out, n = UNRELEASED_LINK_RE.subn(footer_links, out, count=1)
    if n != 1:
        raise ReleaseError("CHANGELOG.md footer needs `[Unreleased]: .../compare/vX.Y.Z...HEAD`")
    return out


def retarget(text: str, pattern: str, new: str) -> tuple[str, int]:
    """Replace the capture group of every ``pattern`` match with ``new``; return ``(text, count)``."""

    def repl(m: re.Match[str]) -> str:
        s, e, o = m.span(1)[0], m.span(1)[1], m.start()
        return m.group(0)[: s - o] + new + m.group(0)[e - o :]

    return re.subn(pattern, repl, text, flags=re.MULTILINE)


def stale_mentions(old: str) -> list[str]:
    """Other tracked-file mentions of the old version that ``cut`` does not move (review by hand)."""
    proc = run(
        ["git", "grep", "-n", "-I", "-E", rf"(^|[^0-9.]){re.escape(old)}($|[^0-9]|\.($|[^0-9]))", "--", ".",
         ":!CHANGELOG.md", ":!docs/changelog", ":!vendor", ":!notebooks", ":!experiments"],
        check=False,
    )
    hits: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        rel, no, text = line.split(":", 2)
        handled = any(
            (m := re.search(p, text)) and m[1] == old for p in VERSION_LOCATIONS.get(rel, [])
        )
        if not handled:
            hits.setdefault(rel, []).append(no)
    return [f"{rel}:{','.join(nos)} still mentions {old} — not moved by cut, review" for rel, nos in hits.items()]


def preflight_sync(new_changelog: str) -> None:
    """Prove the GitBook mirror accepts the stamped CHANGELOG, in a scratch copy, before anything is written."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        (scratch / SYNC_SCRIPT).parent.mkdir(parents=True)
        shutil.copy(REPO_ROOT / SYNC_SCRIPT, scratch / SYNC_SCRIPT)
        (scratch / "CHANGELOG.md").write_text(new_changelog, encoding="utf-8")
        proc = run([sys.executable, str(scratch / SYNC_SCRIPT)], check=False)
    if proc.returncode != 0:
        raise ReleaseError(f"GitBook changelog sync rejects the stamped CHANGELOG: {(proc.stderr or proc.stdout).strip()}")


def cut_plan(version: str, *, today: str, allow_dirty: bool, allow_empty: bool) -> tuple[dict, dict[str, str]]:
    """Plan a cut entirely in memory: return ``(plan, {relative path: new text})``."""
    version = normalize_version(version)
    try:
        if not DATE_RE.match(today):
            raise ValueError(today)
        _dt.date.fromisoformat(today)
    except ValueError as exc:
        raise ReleaseError(f"--date {today!r} is not a real date in YYYY-MM-DD form") from exc
    state = chain_state()
    old = state["version"]
    if state["dirty"] and not allow_dirty:
        raise ReleaseError("working tree is dirty — commit/stash or pass --allow-dirty")
    if state["unreleased_empty"] and not allow_empty:
        raise ReleaseError("[Unreleased] is empty — pass --allow-empty to force")
    if any(t["version"] == version for t in state["tags"]):
        raise ReleaseError(f"tag {tag_for(version)} already exists")
    if version in state["sections"] or tag_for(version) in state["sections"]:
        raise ReleaseError(f"CHANGELOG already has [{tag_for(version)}]")
    floor = max([parse_semver(old), *(parse_semver(t["version"]) for t in state["tags"])])
    if parse_semver(version) <= floor:
        raise ReleaseError(f"{version} must be greater than {'.'.join(map(str, floor))} (pyproject / newest tag)")

    edits = {"CHANGELOG.md": stamp_changelog(changelog_text(), version, today)}
    preflight_sync(edits["CHANGELOG.md"])
    for rel, patterns in VERSION_LOCATIONS.items():
        if not (REPO_ROOT / rel).is_file():
            raise ReleaseError(f"{rel}: missing (listed in VERSION_LOCATIONS)")
        text = read(rel)
        for pattern in patterns:
            text, count = retarget(text, pattern, version)
            if not count:
                raise ReleaseError(f"{rel}: no match for /{pattern}/ — fix VERSION_LOCATIONS or the file")
        edits[rel] = text

    changes = [{"path": "CHANGELOG.md", "op": "stamp", "to": f"## [{tag_for(version)}] - {today}"}]
    changes += [{"path": rel, "op": "bump", "from": old, "to": version} for rel in VERSION_LOCATIONS]
    changes.append({"path": "docs/changelog/", "op": "regenerate", "via": SYNC_SCRIPT})
    plan = {
        "action": "cut",
        "version": version,
        "tag": tag_for(version),
        "date": today,
        "from_version": old,
        "changes": changes,
        "warnings": stale_mentions(old),
        "next": [
            "re-run with --apply to write",
            "review `git diff`, run the tests, commit on a release branch / PR",
            f"after merge: {SCRIPT} publish {version} --commit <merge-sha> --apply --push",
        ],
        "exit_code": 0,
    }
    return plan, edits


def cut_apply(edits: dict[str, str]) -> None:
    """Write every planned edit, then regenerate ``docs/changelog/``; restore the edited files on any failure."""
    originals = {rel: read(rel) for rel in edits}
    try:
        for rel, text in edits.items():
            (REPO_ROOT / rel).write_text(text, encoding="utf-8")
        proc = run_sync(check=False)
        if proc.returncode != 0:
            raise ReleaseError(f"{SYNC_SCRIPT} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    except Exception as exc:
        unrestored = []
        for rel, text in originals.items():  # best effort: one failing file must not stop the others
            try:
                (REPO_ROOT / rel).write_text(text, encoding="utf-8")
            except OSError:
                unrestored.append(rel)
        tail = f"could not restore {unrestored}" if unrestored else "edited files restored"
        raise ReleaseError(f"{exc} — {tail}; check `git status` (docs/changelog/ is regenerated by the sync)") from exc


# ---------------------------------------------------------------------------
# notes / publish
# ---------------------------------------------------------------------------


def previous_tag(version: str, tags: list[dict]) -> str | None:
    """The highest version tag below ``version``."""
    target = parse_semver(version)
    return next((t["tag"] for t in tags if parse_semver(t["version"]) < target), None)


def merged_prs(prev: str | None, end: str) -> list[str]:
    """Bullet lines for the pull requests merged in ``prev..end`` (from merge commits, offline)."""
    out = git("log", "--merges", "--format=%s%x1f%b%x1e", f"{prev}..{end}" if prev else end)
    bullets = []
    for record in filter(str.strip, out.split("\x1e")):
        subject, _, body = record.partition("\x1f")
        m = PR_MERGE_RE.search(subject)
        if m:
            title = next((ln.strip() for ln in body.splitlines() if ln.strip()), m["branch"])
            bullets.append(f"- {title} — #{m['num']}")
    return bullets


def build_notes(version: str, *, commit_ref: str | None = None, previous: str | None = None) -> dict:
    """Render GitHub release notes for a version from its CHANGELOG section and merge history."""
    version = normalize_version(version)
    tag = tag_for(version)
    tags = list_version_tags()
    ref = commit_ref or (tag if any(t["tag"] == tag for t in tags) else None)
    section = next((s for s in parse_changelog(changelog_text(ref)) if s["version"] == version), None)
    if section is None:
        raise ReleaseError(f"no ## [{tag}] section in CHANGELOG.md — cut first")
    commit = git("rev-parse", f"{ref or 'HEAD'}^{{commit}}").strip()
    end = ref or "HEAD"
    prev = previous or previous_tag(version, tags)
    prs = merged_prs(prev, end) if prev else []
    parts = [f"# llm-mailroom {tag}", "", section["body"], ""]
    if prev:
        parts += ["## Pull requests merged", "", *(prs or ["None."]), ""]
    parts += [
        f"**Changelog:** [CHANGELOG.md]({REPO_URL}/blob/main/CHANGELOG.md)  ",
        f"**Compare:** {REPO_URL}/compare/{prev}...{tag}  " if prev else f"**First release:** {tag}  ",
        f"**Release commit:** `{commit}`",
    ]
    return {"version": version, "tag": tag, "title": f"llm-mailroom {tag}", "previous": prev,
            "commit": commit, "body": "\n".join(parts) + "\n"}


def remote_tag_commit(tag: str) -> str | None:
    """Commit a tag points at on origin (peeled), or ``None``; unreachable origin is a refusal."""
    proc = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}*"], check=False)
    if proc.returncode != 0:
        raise ReleaseError(f"cannot query origin for {tag}: {proc.stderr.strip()}")
    shas = {ref: sha for sha, _, ref in (ln.partition("\t") for ln in proc.stdout.splitlines())}
    return shas.get(f"refs/tags/{tag}^{{}}") or shas.get(f"refs/tags/{tag}")


def remote_release(tag: str) -> dict | None:
    """The GitHub release of a tag, or ``None`` when there is none (any other gh failure is a refusal)."""
    proc = run(["gh", "release", "view", tag, "--repo", REPO, "--json", "tagName,isDraft,url"], check=False)
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    if "release not found" in proc.stderr.lower():
        return None
    raise ReleaseError(f"gh release view {tag} failed: {proc.stderr.strip()}")


def publish_plan(version: str, *, commit_ref: str | None, push: bool, apply: bool = False, allow_unmerged: bool = False) -> dict:
    """Plan tag + push + GitHub release for a version, collecting every refusal in ``errors``."""
    version = normalize_version(version)
    tag = tag_for(version)
    commit = git("rev-parse", "--verify", f"{commit_ref or 'HEAD'}^{{commit}}").strip()
    state = chain_state()
    chain_errors, warnings = check_chain(state)
    errors: list[str] = []
    # Chain checks describe the working tree; they only gate a release cut from it.
    if commit == state["head"]:
        errors += chain_errors
    else:
        warnings += [f"working tree: {e}" for e in chain_errors]

    committed = pyproject_version(git("show", f"{commit}:pyproject.toml"))
    if committed != version:
        errors.append(f"commit {commit[:12]} pyproject version {committed} != {version}")
    section = next((s for s in parse_changelog(changelog_text(commit)) if s["version"] == version), None)
    if section is None:
        errors.append(f"commit {commit[:12]} CHANGELOG.md has no [{tag}] section — cut and merge first")
    elif not section["stamp"]:
        errors.append(f"commit {commit[:12]} CHANGELOG [{tag}] has no date")

    local = next((t for t in state["tags"] if t["version"] == version), None)
    remote = remote_tag_commit(tag)
    if local and local["commit"] != commit:
        errors.append(f"local tag {tag} points at {local['commit'][:12]}, not {commit[:12]}")
    if remote and remote != commit:
        errors.append(f"origin {tag} points at {remote[:12]}, not {commit[:12]}")
    existing = remote_release(tag)
    if existing:
        errors.append(f"GitHub release {tag} already exists at {existing.get('url')}")
    if run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], check=False).returncode != 0:
        msg = f"commit {commit[:12]} is not on origin/main (merge it, or `git fetch origin` if it just merged)"
        if apply and not allow_unmerged:
            errors.append(f"{msg}; pass --allow-unmerged to tag it anyway")
        else:
            warnings.append(f"{msg}; --apply refuses unless --allow-unmerged")

    notes = build_notes(version, commit_ref=commit) if section else {"title": f"llm-mailroom {tag}", "body": ""}
    steps = []
    if not local:
        steps.append({"op": "git_tag", "tag": tag, "commit": commit, "message": f"Release {tag}"})
    if push and not remote:
        steps.append({"op": "git_push_tag", "tag": tag})
    if remote or push:  # `gh release create --verify-tag`: the tag must already be on origin
        steps.append({"op": "gh_release_create", "tag": tag, "title": notes["title"]})
    return {
        "action": "publish",
        "version": version,
        "tag": tag,
        "commit": commit,
        "push": push,
        "notes_title": notes["title"],
        "notes_body": notes["body"],
        "steps": steps,
        "errors": errors,
        "warnings": warnings,
        "next": ["re-run with --apply"]
        + ([] if remote or push else ["add --push to push the tag to origin and create the GitHub release"]),
        "exit_code": 1 if errors else 0,
    }


def publish_apply(plan: dict) -> dict:
    """Execute a publish plan: annotated tag, plain tag push, GitHub release. Nothing is forced."""
    if plan["errors"]:
        raise ReleaseError("; ".join(plan["errors"]))
    tag, results = plan["tag"], []
    for step in plan["steps"]:
        if step["op"] == "git_tag":
            run(["git", "tag", "-a", tag, plan["commit"], "-m", step["message"]])
        elif step["op"] == "git_push_tag":
            run(["git", "push", "origin", f"refs/tags/{tag}"])
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
                handle.write(plan["notes_body"])
            try:
                run(["gh", "release", "create", tag, "--repo", REPO, "--verify-tag",
                     "--title", plan["notes_title"], "--notes-file", handle.name])
            finally:
                os.unlink(handle.name)
        results.append({"op": step["op"], "tag": tag, "ok": True})
    release = remote_release(tag) if any(s["op"] == "gh_release_create" for s in plan["steps"]) else None
    return {
        "action": "publish", "version": plan["version"], "tag": tag, "commit": plan["commit"],
        "results": results, "release": release, "exit_code": 0,
        "next": [f"verify {release['url']}" if release else f"{SCRIPT} publish {plan['version']} --commit {plan['commit']} --apply --push to push the tag and create the release"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """``status``: print the release-chain identity."""
    state = chain_state()
    nxt = []
    if not any(t["version"] == state["version"] for t in state["tags"]):
        nxt.append(f"{SCRIPT} publish {state['version']} --commit <release-commit>   (version is untagged)")
    if not state["unreleased_empty"]:
        nxt.append(f"{SCRIPT} cut X.Y.Z   (dry run; [Unreleased] has content)")
    lines = [
        f"repo               : {state['repo']}",
        f"HEAD               : {state['head'][:12]}{' (dirty)' if state['dirty'] else ''}",
        f"pyproject version  : {state['version']}",
        f"newest changelog   : {state['newest_changelog_version']}",
        f"newest version tag : {state['newest_tag'] or '(none)'}",
        f"unreleased         : {'empty' if state['unreleased_empty'] else 'content pending release'}",
        *(f"    {t['tag']:<10} {t['type']:<7} {t['commit'][:12]}  {t['subject']}" for t in state["tags"][:8]),
    ]
    return emit({"action": "status", **state, "exit_code": 0, "next": nxt}, as_json=args.json, human_lines=lines)


def cmd_check(args: argparse.Namespace) -> int:
    """``check``: validate the release chain; exit 1 on errors."""
    state = chain_state()
    errors, warnings = check_chain(state)
    payload = {
        "action": "check", "ok": not errors, "errors": errors, "warnings": warnings,
        "state": {k: state[k] for k in ("version", "newest_tag", "head", "dirty")},
        "next": ["fix the errors before cut/publish"] if errors else [],
        "exit_code": 1 if errors else 0,
    }
    lines = [f"WARN  {w}" for w in warnings] + [f"ERROR {e}" for e in errors]
    lines.append(f"\nrelease chain: {'FAIL' if errors else 'OK'} ({len(errors)} error(s), {len(warnings)} warning(s))")
    return emit(payload, as_json=args.json, human_lines=lines)


def cmd_cut(args: argparse.Namespace) -> int:
    """``cut``: stamp CHANGELOG and move every version location (dry run unless ``--apply``)."""
    plan, edits = cut_plan(
        args.version,
        today=args.date or _dt.date.today().isoformat(),
        allow_dirty=args.allow_dirty,
        allow_empty=args.allow_empty,
    )
    plan["dry_run"] = not args.apply
    lines = [f"cut {plan['from_version']} -> {plan['version']} ({plan['date']}):"]
    lines += [f"  {c['path']}: {c['op']}" for c in plan["changes"]]
    lines += [f"WARN  {w}" for w in plan["warnings"]]
    if args.apply:
        cut_apply(edits)
        plan["applied"] = True
        plan["next"] = plan["next"][1:]
        lines.append(f"\napplied cut for v{plan['version']}")
    else:
        lines.append("\ndry run — pass --apply to write")
    return emit(plan, as_json=args.json, human_lines=lines)


def cmd_notes(args: argparse.Namespace) -> int:
    """``notes``: render release notes for a version."""
    notes = build_notes(args.version, commit_ref=args.commit)
    if args.out:
        Path(args.out).write_text(notes["body"], encoding="utf-8")
    payload = {"action": "notes", **notes, "out": args.out, "exit_code": 0,
               "next": [f"publish uses the same notes; {SCRIPT} publish {notes['version']}"]}
    return emit(payload, as_json=args.json, human_lines=[f"wrote {args.out}" if args.out else notes["body"]])


def cmd_publish(args: argparse.Namespace) -> int:
    """``publish``: tag and create the GitHub release (dry run unless ``--apply``)."""
    plan = publish_plan(
        args.version, commit_ref=args.commit, push=args.push, apply=args.apply, allow_unmerged=args.allow_unmerged
    )
    plan["dry_run"] = not args.apply
    lines = [f"ERROR {e}" for e in plan["errors"]] + [f"WARN  {w}" for w in plan["warnings"]]
    if plan["errors"]:
        return emit(plan, as_json=args.json, human_lines=lines)
    if not args.apply:
        lines += [f"publish plan — {plan['tag']} @ {plan['commit'][:12]}", *(f"  - {s}" for s in plan["steps"])]
        return emit(plan, as_json=args.json, human_lines=lines + ["\ndry run — pass --apply"])
    result = publish_apply(plan)
    lines = [f"published {result['tag']} @ {result['commit'][:12]}", *(f"  - {r['op']}" for r in result["results"])]
    return emit(result, as_json=args.json, human_lines=lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="release.py", description="llm-mailroom release governor.")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, func, help_: str, *, version: bool = False, apply: bool = False):
        p = sub.add_parser(name, help=help_)
        if version:
            p.add_argument("version")
        if apply:
            p.add_argument("--apply", action="store_true", help="write (default is a dry run)")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)
        return p

    command("status", cmd_status, "chain snapshot")
    command("check", cmd_check, "verify changelog / version / tag consistency")
    p = command("cut", cmd_cut, "stamp [Unreleased] and move every version location", version=True, apply=True)
    p.add_argument("--date", help="stamp date YYYY-MM-DD (default: today)")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-empty", action="store_true")
    p = command("notes", cmd_notes, "render release notes from CHANGELOG", version=True)
    p.add_argument("--commit", help="release commit (default: working tree / HEAD)")
    p.add_argument("--out", help="write the notes to FILE")
    p = command("publish", cmd_publish, "annotated tag + GitHub release", version=True, apply=True)
    p.add_argument("--commit", help="release commit to tag (default HEAD)")
    p.add_argument("--push", action="store_true", help="push the tag to origin and create the GitHub release")
    p.add_argument("--allow-unmerged", action="store_true", help="--apply even if the commit is not on origin/main")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; a ``ReleaseError`` becomes an error message and exit code 1."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "exit_code": 1, "next": []}, indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.environ.setdefault("GH_PROMPT_DISABLED", "1")
    raise SystemExit(main())
