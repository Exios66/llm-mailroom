#!/usr/bin/env python3
"""Mirror repository CHANGELOG.md into the GitBook Changelog space.

GitBook Git Sync publishes ``docs/changelog/`` as a separate site section
(``docs/gitbook-docs.yaml`` → Changelog / ``space-1``). That space shipped
with GitBook's onboarding "Product update" placeholders. This script is the
only writer for those pages: it parses the Keep a Changelog file at the
repository root and regenerates the GitBook ``{% updates %}`` feed plus one
page per release.

    PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py
    PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py --check
    PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py --dry-run

Do not hand-edit ``docs/changelog/**/*.md``. ``docs/changelog/.gitbook/tags.yaml``
is GitBook-owned (feature / improvement / fix) and is left alone.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
CHANGELOG_DIR = REPO_ROOT / "docs" / "changelog"
CANONICAL_URL = "https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md"
DOCS_URL = "https://mailroom-inc.gitbook.io/mailroom-inc.-docs/"

HEADING_RE = re.compile(
    r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)
FOOTER_RE = re.compile(r"\n\[Unreleased\]:\s+https://")
SECTION_RE = re.compile(
    r"^### (Added|Changed|Deprecated|Removed|Fixed|Security)\s*$",
    re.MULTILINE,
)

SECTION_TAG = {
    "Added": "feature",
    "Changed": "improvement",
    "Deprecated": "improvement",
    "Removed": "improvement",
    "Fixed": "fix",
    "Security": "fix",
}
TAG_ORDER = ("feature", "improvement", "fix")

# Undated catch-up heading in CHANGELOG.md sits between v0.3.0 (2026-08-19)
# and 0.2.2 (2026-08-08). Pin a calendar day so GitBook's updates block can
# sort it; the page title keeps the original Keep a Changelog label.
UNDATED_DATES = {
    "Released backlog — v0.4.0 → v0.6.0 era": "2026-08-18",
}

README_FRONTMATTER = """\
---
description: llm-mailroom releases, synced from CHANGELOG.md
icon: clock-rotate-left
layout:
  width: wide
  title:
    visible: true
  description:
    visible: true
  tableOfContents:
    visible: false
  outline:
    visible: true
  pagination:
    visible: false
  metadata:
    visible: false
  tags:
    visible: true
  actions:
    visible: true
  anchors:
    visible: true
---
"""

HINT = f"""\
{{% hint style="info" %}}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`]({CANONICAL_URL}) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{{% endhint %}}
"""


@dataclass(frozen=True)
class Release:
    title: str
    date: str
    body: str
    tags: tuple[str, ...]
    slug: str
    year: str
    page_rel: str

    @property
    def is_unreleased(self) -> bool:
        return self.title.lower() == "unreleased"


def _slug(title: str) -> str:
    if title.lower() == "unreleased":
        return "unreleased"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "release"


def _tags_for(body: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in SECTION_RE.finditer(body):
        tag = SECTION_TAG[match.group(1)]
        if tag not in found:
            found.append(tag)
    ordered = tuple(tag for tag in TAG_ORDER if tag in found)
    return ordered or ("improvement",)


def changelog_source_date(path: Path = CHANGELOG_PATH) -> str:
    """Stable ISO date for the Unreleased card (last CHANGELOG.md commit)."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", str(path.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        stamp = (out.stdout or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
            return stamp
    except (OSError, subprocess.SubprocessError):
        pass
    return _dt.date.today().isoformat()


def parse_changelog(text: str, *, unreleased_date: str) -> list[Release]:
    text = FOOTER_RE.split(text, maxsplit=1)[0]
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        raise SystemExit("CHANGELOG.md has no Keep a Changelog headings")
    releases: list[Release] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        date = match.group(2)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not date:
            if title.lower() == "unreleased":
                date = unreleased_date
            else:
                date = UNDATED_DATES.get(title)
            if not date:
                raise SystemExit(f"CHANGELOG heading {title!r} has no date")
        slug = _slug(title)
        year = date[:4]
        page_rel = f"{slug}.md" if title.lower() == "unreleased" else f"{year}/{slug}.md"
        releases.append(
            Release(
                title=title,
                date=date,
                body=body,
                tags=_tags_for(body),
                slug=slug,
                year=year,
                page_rel=page_rel,
            )
        )
    return releases


def _tag_csv(tags: tuple[str, ...]) -> str:
    return ",".join(tags)


def _tag_frontmatter(tags: tuple[str, ...]) -> str:
    lines = "\n".join(f"  - {tag}" for tag in tags)
    return f"---\ntags:\n{lines}\n---\n"


def _page_href(release: Release) -> str:
    return release.page_rel


def render_release_page(release: Release) -> str:
    heading = release.title
    dated = (
        "Unreleased work on `main`."
        if release.is_unreleased
        else f"Released {release.date}."
    )
    return (
        f"{_tag_frontmatter(release.tags)}\n"
        f"# {heading}\n\n"
        f"{HINT}\n"
        f"{dated} Canonical source: [`CHANGELOG.md`]({CANONICAL_URL}).\n\n"
        f"{release.body.strip()}\n"
    )


def _feed_update(release: Release) -> str:
    href = _page_href(release)
    github = CANONICAL_URL
    summary = (
        "Work landed on `main` since the last tagged release."
        if release.is_unreleased
        else f"llm-mailroom {release.title}, released {release.date}."
    )
    return (
        f"{{% update date=\"{release.date}\" tags=\"{_tag_csv(release.tags)}\" %}}\n"
        f"## {release.title}\n\n"
        f"{summary}\n\n"
        f"{release.body.strip()}\n\n"
        f"<a href=\"{href}\" class=\"button primary\">Read full update</a>"
        f"<a href=\"{github}\" class=\"button secondary\">CHANGELOG.md on GitHub</a>\n"
        f"{{% endupdate %}}\n"
    )


def render_readme(releases: list[Release]) -> str:
    updates = "\n".join(_feed_update(rel) for rel in releases)
    return (
        f"{README_FRONTMATTER}\n"
        f"# Changelog\n\n"
        f"{HINT}\n"
        f"Every entry below is copied from llm-mailroom's Keep a Changelog file. "
        f"Tagged GitHub releases stay canonical; this space is the GitBook view of "
        f"the same list. Pipeline docs: [{DOCS_URL}]({DOCS_URL}).\n\n"
        f"{{% updates format=\"full\" %}}\n"
        f"{updates}"
        f"{{% endupdates %}}\n"
    )


def render_summary(releases: list[Release]) -> str:
    lines = [
        "# Table of contents",
        "",
        "* [Changelog](README.md)",
        "* [Unreleased](unreleased.md)",
        "",
    ]
    by_year: dict[str, list[Release]] = {}
    for release in releases:
        if release.is_unreleased:
            continue
        by_year.setdefault(release.year, []).append(release)
    for year in sorted(by_year, reverse=True):
        lines.append(f"## {year}")
        lines.append("")
        for release in by_year[year]:
            lines.append(f"* [{release.title}]({release.page_rel})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def expected_files(
    changelog_text: str | None = None,
    *,
    unreleased_date: str | None = None,
) -> dict[str, str]:
    text = changelog_text if changelog_text is not None else CHANGELOG_PATH.read_text(encoding="utf-8")
    stamp = unreleased_date or changelog_source_date()
    releases = parse_changelog(text, unreleased_date=stamp)
    files = {
        "README.md": render_readme(releases),
        "SUMMARY.md": render_summary(releases),
    }
    for release in releases:
        files[release.page_rel] = render_release_page(release)
    return files


def managed_markdown_paths(changelog_dir: Path = CHANGELOG_DIR) -> set[Path]:
    paths: set[Path] = set()
    if not changelog_dir.is_dir():
        return paths
    for path in changelog_dir.rglob("*.md"):
        paths.add(path)
    return paths


def write_files(files: dict[str, str], *, changelog_dir: Path = CHANGELOG_DIR) -> None:
    changelog_dir.mkdir(parents=True, exist_ok=True)
    expected = {changelog_dir / rel for rel in files}
    for stale in managed_markdown_paths(changelog_dir) - expected:
        stale.unlink()
    for rel, content in files.items():
        target = changelog_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for year_dir in changelog_dir.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit() and not any(year_dir.iterdir()):
            year_dir.rmdir()


def check_files(files: dict[str, str], *, changelog_dir: Path = CHANGELOG_DIR) -> list[str]:
    errors: list[str] = []
    expected = {changelog_dir / rel for rel in files}
    actual = managed_markdown_paths(changelog_dir)
    for extra in sorted(actual - expected):
        errors.append(f"stale GitBook changelog page: {extra.relative_to(REPO_ROOT)}")
    for rel, content in sorted(files.items()):
        target = changelog_dir / rel
        if not target.is_file():
            errors.append(f"missing GitBook changelog page: docs/changelog/{rel}")
            continue
        on_disk = target.read_text(encoding="utf-8")
        if on_disk != content:
            errors.append(f"stale GitBook changelog page: docs/changelog/{rel}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/changelog/ does not match CHANGELOG.md",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the files that would be written",
    )
    args = parser.parse_args(argv)

    if not CHANGELOG_PATH.is_file():
        print("missing CHANGELOG.md", file=sys.stderr)
        return 1
    files = expected_files()
    if args.check:
        errors = check_files(files)
        if errors:
            print("GitBook changelog is out of date:", file=sys.stderr)
            for error in errors:
                print(f"  {error}", file=sys.stderr)
            print(
                "Regenerate with: PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py",
                file=sys.stderr,
            )
            return 1
        print(f"ok: {len(files)} GitBook changelog pages match CHANGELOG.md")
        return 0
    if args.dry_run:
        for rel in sorted(files):
            print(rel)
        return 0
    write_files(files)
    print(f"wrote {len(files)} pages under docs/changelog/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
