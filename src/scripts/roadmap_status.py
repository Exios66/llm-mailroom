"""Status, alignment check and issue body for the unified audit row roadmap.

Reads the stage plans in ``docs/superpowers/plans/unified-audit-row/`` and the spec
headings, then:

- ``--write``       regenerates the status table in the roadmap README;
- ``--check``       exits 1 when the table is stale or the plans and spec disagree
                    (unknown or uncited spec sections, unknown dependencies, a task
                    done before a task it depends on);
- ``--issue-body``  prints the tracking issue body (used by roadmap-sync.yml).

Stdlib only and network-free; the workflow does the GitHub call.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_DIR = REPO_ROOT / "docs/superpowers/plans/unified-audit-row"
SPEC_PATH = REPO_ROOT / "docs/superpowers/specs/2026-11-03-unified-audit-row-design.md"
REPO_URL = "https://github.com/Exios66/llm-mailroom/blob/main"

#: Goal and staleness sections need no implementing task.
EXEMPT_SECTIONS = frozenset({"1", "2"})
#: GitHub's issue body limit.
ISSUE_LIMIT = 65_536

BEGIN = "<!-- roadmap:status:begin -->"
END = "<!-- roadmap:status:end -->"

_TASK_ANY = re.compile(r"^### Task\b(.*)$")
_TASK = re.compile(r"^### Task ([A-Z]\d+): (.+?)\s*$")
_FIELD = re.compile(r"^\*\*(Repo|Spec|Depends):\*\*\s*(.*)$")
_STEP = re.compile(r"^- \[( |x|X)\] ")
_SECTION_REF = re.compile(r"§(\d+(?:\.\d+)*)")
_TASK_REF = re.compile(r"\b([A-Z]\d+)\b")
_HEADING = re.compile(r"^#{2,6} (\d+(?:\.\d+)*)\.?\s")
_STAGE_FILE = re.compile(r"^stage-(\d+)-.*\.md$")


class PlanError(ValueError):
    """A plan file does not follow the task format."""


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    stage: int
    file: str
    repo: str
    spec: tuple[str, ...]
    depends: tuple[str, ...]
    done_steps: int
    total_steps: int
    has_depends: bool = False

    @property
    def done(self) -> bool:
        return self.total_steps > 0 and self.done_steps == self.total_steps


def _parse_file(path: Path, stage: int) -> list[Task]:
    tasks: list[Task] = []
    current: dict | None = None

    def flush() -> None:
        if current is not None:
            tasks.append(Task(**current))

    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if _TASK_ANY.match(line):
            m = _TASK.match(line)
            if not m:
                raise PlanError(f"{path.name}:{lineno}: task heading needs '### Task <ID>: <name>'")
            flush()
            current = {
                "id": m.group(1), "name": m.group(2), "stage": stage, "file": path.name,
                "repo": "", "spec": (), "depends": (), "done_steps": 0, "total_steps": 0,
            }
            continue
        if current is None:
            continue
        if line.startswith("## ") or line.startswith("### "):
            flush()
            current = None
            continue
        field = _FIELD.match(line)
        if field:
            key, value = field.group(1), field.group(2)
            if key == "Repo":
                current["repo"] = value.strip()
            elif key == "Spec":
                current["spec"] = tuple(_SECTION_REF.findall(value))
            else:
                current["has_depends"] = True
                current["depends"] = tuple(_TASK_REF.findall(value))
            continue
        step = _STEP.match(line)
        if step:
            current["total_steps"] += 1
            if step.group(1) != " ":
                current["done_steps"] += 1
    flush()
    return tasks


def parse_plans(plan_dir: Path = PLAN_DIR) -> list[Task]:
    tasks: list[Task] = []
    for path in sorted(plan_dir.glob("stage-*.md")):
        m = _STAGE_FILE.match(path.name)
        if not m:
            raise PlanError(f"{path.name}: stage plans must be named 'stage-<N>-<slug>.md'")
        tasks.extend(_parse_file(path, int(m.group(1))))
    return tasks


def spec_sections(spec: Path = SPEC_PATH) -> set[str]:
    return {m.group(1) for line in spec.read_text().splitlines() if (m := _HEADING.match(line))}


def task_state(task: Task, by_id: dict[str, Task]) -> str:
    if task.done:
        return "done"
    if task.done_steps:
        return "in progress"
    if all(by_id[d].done for d in task.depends if d in by_id):
        return "ready"
    return "blocked"


def render_status(tasks: list[Task]) -> str:
    by_id = {t.id: t for t in tasks}
    done = sum(t.done for t in tasks)
    lines = [
        f"**{done}/{len(tasks)} tasks done.**",
        "",
        "| Stage | Task | Name | Repo | Steps | State |",
        "|---|---|---|---|---|---|",
    ]
    for t in tasks:
        lines.append(
            f"| {t.stage} | {t.id} | {_cell(t.name)} | {_cell(t.repo)} | {t.done_steps}/{t.total_steps} | {task_state(t, by_id)} |"
        )
    return "\n".join(lines)


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|")


def _block(text: str) -> tuple[int, int]:
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        raise PlanError(f"README.md: status markers {BEGIN} / {END} missing")
    return start + len(BEGIN), end


def write_status(plan_dir: Path = PLAN_DIR) -> bool:
    readme = plan_dir / "README.md"
    text = readme.read_text()
    a, b = _block(text)
    new = text[:a] + "\n" + render_status(parse_plans(plan_dir)) + "\n" + text[b:]
    if new != text:
        readme.write_text(new)
        return True
    return False


def check(plan_dir: Path = PLAN_DIR, spec: Path = SPEC_PATH) -> list[str]:
    errors: list[str] = []
    try:
        tasks = parse_plans(plan_dir)
    except PlanError as exc:
        return [str(exc)]
    by_id: dict[str, Task] = {}
    for t in tasks:
        if t.id in by_id:
            errors.append(f"{t.id}: duplicate task id ({by_id[t.id].file}, {t.file})")
        by_id[t.id] = t
        if not t.repo:
            errors.append(f"{t.id}: missing **Repo:** line")
        if not t.spec:
            errors.append(f"{t.id}: missing **Spec:** section reference")
        if not t.total_steps:
            errors.append(f"{t.id}: no steps")
        if not t.has_depends:
            errors.append(f"{t.id}: missing **Depends:** line (write 'none' if it has no dependency)")

    sections = spec_sections(spec)
    cited: set[str] = set()
    for t in tasks:
        for ref in t.spec:
            cited.add(ref)
            if ref not in sections:
                errors.append(f"{t.id}: cites §{ref}, which is not a section of the spec")
        for dep in t.depends:
            if dep not in by_id:
                errors.append(f"{t.id}: depends on unknown task {dep}")
            elif t.done_steps and not by_id[dep].done:
                errors.append(f"{t.id}: progress recorded before its dependency {dep} is done")

    for sec in sorted(sections, key=lambda s: tuple(int(p) for p in s.split("."))):
        if sec.split(".")[0] in EXEMPT_SECTIONS:
            continue
        covered = sec in cited or any(c.startswith(sec + ".") for c in cited)
        if not covered:
            errors.append(f"§{sec} has no task citing it")

    readme = plan_dir / "README.md"
    text = readme.read_text()
    try:
        a, b = _block(text)
    except PlanError as exc:
        errors.append(str(exc))
    else:
        if text[a:b].strip() != render_status(tasks).strip():
            errors.append("README.md status table is stale: run `python src/scripts/roadmap_status.py --write`")
    return errors


def render_issue_body(tasks: list[Task], readme: Path = PLAN_DIR / "README.md") -> str:
    plans_url = f"{REPO_URL}/docs/superpowers/plans/unified-audit-row"
    spec_url = f"{REPO_URL}/docs/superpowers/specs/{SPEC_PATH.name}"
    head = "\n".join([
        "Tracks the unified audit row work: one hash-chained row per document, with attempt "
        "sub-rows, addenda, a legacy section and tiered per-document-type scoring.",
        "",
        f"- **Spec:** [{SPEC_PATH.name}]({spec_url})",
        f"- **Roadmap and stage plans:** [{plans_url.rsplit('/', 1)[-1]}/]({plans_url}) "
        "(see the README there for stages, gates and the adversarial review)",
        "- **Related:** LLM-Mailroom-Services/mailroom-issues #235, #236, #237, #238, #239, #240, #241",
        "",
        "_This body is generated by `src/scripts/roadmap_status.py --issue-body` on every push to "
        "`main` that touches the plans (`.github/workflows/roadmap-sync.yml`). Edit the plans, "
        "not this issue._",
        "",
        "## Status",
        "",
    ])
    foot = "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"
    body = head + render_status(tasks) + foot
    if len(body) > ISSUE_LIMIT:
        note = f"\n\n(Task table truncated; see the [roadmap README]({plans_url}/README.md).)"
        room = ISSUE_LIMIT - len(head) - len(foot) - len(note)
        table = render_status(tasks)[:room]
        table = table[: table.rfind("\n")] if "\n" in table else table
        body = head + table + note + foot
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--issue-body", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        changed = write_status()
        print("updated" if changed else "unchanged")
        return 0
    if args.check:
        errors = check()
        for e in errors:
            print(f"roadmap: {e}", file=sys.stderr)
        if not errors:
            print("roadmap: OK")
        return 1 if errors else 0
    print(render_issue_body(parse_plans()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
