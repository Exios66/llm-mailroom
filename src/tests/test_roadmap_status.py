"""Offline tests for src/scripts/roadmap_status.py (unified audit row roadmap)."""

from pathlib import Path

import pytest

from scripts import roadmap_status as R

SPEC = """# Spec

## 1. Goal
## 2. Staleness
## 3. Scoring
### 3.1 Tiers
## 4. Data
"""

README = """# Roadmap

<!-- roadmap:issue Exios66/llm-mailroom#150 -->

<!-- roadmap:status:begin -->
old
<!-- roadmap:status:end -->
"""

STAGE1 = """# Stage 1

### Task D1: First

**Repo:** llm-dojo-scoring
**Spec:** §3, §3.1
**Depends:** none

- [x] **Step 1: a**
- [x] **Step 2: b**

### Task D2: Second

**Repo:** llm-dojo-scoring
**Spec:** §4
**Depends:** D1

- [x] **Step 1: a**
- [ ] **Step 2: b**
"""


def _tree(tmp_path: Path, stage1: str = STAGE1, spec: str = SPEC) -> tuple[Path, Path]:
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "README.md").write_text(README)
    (plans / "stage-1-dojo.md").write_text(stage1)
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(spec)
    return plans, spec_path


def test_parse_tasks_and_steps(tmp_path):
    plans, _ = _tree(tmp_path)
    tasks = R.parse_plans(plans)
    assert [t.id for t in tasks] == ["D1", "D2"]
    d1, d2 = tasks
    assert d1.stage == 1 and d1.repo == "llm-dojo-scoring"
    assert d1.spec == ("3", "3.1") and d1.depends == ()
    assert (d1.done_steps, d1.total_steps, d1.done) == (2, 2, True)
    assert (d2.done_steps, d2.total_steps, d2.done) == (1, 2, False)
    assert d2.depends == ("D1",)


def test_spec_sections(tmp_path):
    _, spec = _tree(tmp_path)
    assert R.spec_sections(spec) == {"1", "2", "3", "3.1", "4"}


def test_clean_tree_checks_after_write(tmp_path):
    plans, spec = _tree(tmp_path)
    assert any("stale" in e for e in R.check(plans, spec))
    R.write_status(plans)
    assert R.check(plans, spec) == []


def test_missing_spec_section(tmp_path):
    plans, spec = _tree(tmp_path, STAGE1.replace("**Spec:** §4", "**Spec:** §4, §9.9"))
    R.write_status(plans)
    assert any("§9.9" in e and "D2" in e for e in R.check(plans, spec))


def test_uncited_spec_section(tmp_path):
    plans, spec = _tree(tmp_path, spec=SPEC + "## 5. Readers\n")
    R.write_status(plans)
    errors = R.check(plans, spec)
    assert any("§5" in e and "no task" in e for e in errors)
    assert not any("§1 " in e or "§2 " in e for e in errors)


def test_unknown_dependency(tmp_path):
    plans, spec = _tree(tmp_path, STAGE1.replace("**Depends:** D1", "**Depends:** D1, Z9"))
    R.write_status(plans)
    assert any("Z9" in e for e in R.check(plans, spec))


def test_done_before_dependency(tmp_path):
    d1, d2 = STAGE1.split("### Task D2")
    text = d1.replace("- [x] **Step 2: b**", "- [ ] **Step 2: b**") + "### Task D2" + d2.replace("- [ ]", "- [x]")
    plans, spec = _tree(tmp_path, text)
    R.write_status(plans)
    tasks = {t.id: t for t in R.parse_plans(plans)}
    assert tasks["D2"].done and not tasks["D1"].done
    assert any("D2" in e and "D1" in e and "before" in e for e in R.check(plans, spec))


def test_malformed_task_heading(tmp_path):
    plans, spec = _tree(tmp_path, STAGE1.replace("### Task D2: Second", "### Task Second"))
    with pytest.raises(R.PlanError, match="stage-1-dojo.md:"):
        R.parse_plans(plans)


def test_status_table_shows_state(tmp_path):
    plans, _ = _tree(tmp_path)
    table = R.render_status(R.parse_plans(plans))
    assert "| 1 | D1 | First | llm-dojo-scoring | 2/2 | done |" in table
    assert "| 1 | D2 | Second | llm-dojo-scoring | 1/2 | in progress |" in table


def test_blocked_and_ready_states(tmp_path):
    text = STAGE1.replace("- [x]", "- [ ]")
    plans, _ = _tree(tmp_path, text)
    states = {t.id: R.task_state(t, {x.id: x for x in R.parse_plans(plans)}) for t in R.parse_plans(plans)}
    assert states == {"D1": "ready", "D2": "blocked"}


def test_issue_body_links_and_limit(tmp_path):
    plans, _ = _tree(tmp_path)
    tasks = R.parse_plans(plans)
    body = R.render_issue_body(tasks, plans / "README.md")
    assert "docs/superpowers/plans/unified-audit-row" in body
    assert "| 1 | D1 |" in body
    many = tasks * 2000
    assert len(R.render_issue_body(many, plans / "README.md")) <= R.ISSUE_LIMIT


def test_repo_roadmap_is_consistent():
    """The real plans and spec in this repository pass the check."""
    assert R.check(R.PLAN_DIR, R.SPEC_PATH) == []


def test_missing_depends_line(tmp_path):
    """A task without a **Depends:** line is an error, not silently dependency-free."""
    plans, spec = _tree(tmp_path, STAGE1.replace("**Depends:** D1\n", ""))
    R.write_status(plans)
    assert any("D2" in e and "Depends" in e for e in R.check(plans, spec))


def test_misnamed_stage_file_is_an_error(tmp_path):
    """A stage-*.md file the parser cannot number is reported, never skipped."""
    plans, _ = _tree(tmp_path)
    (plans / "stage-5.md").write_text(STAGE1.replace("D1", "E1").replace("D2", "E2"))
    with pytest.raises(R.PlanError, match="stage-5.md"):
        R.parse_plans(plans)


def test_spec_sections_any_depth(tmp_path):
    _, spec = _tree(tmp_path, spec=SPEC + "#### 3.1.2 Deep\n")
    assert "3.1.2" in R.spec_sections(spec)


def test_deep_section_reference_parses(tmp_path):
    plans, spec = _tree(tmp_path, STAGE1.replace("**Spec:** §3, §3.1", "**Spec:** §3, §3.1, §3.1.2"),
                        spec=SPEC + "#### 3.1.2 Deep\n")
    assert R.parse_plans(plans)[0].spec == ("3", "3.1", "3.1.2")
    R.write_status(plans)
    assert R.check(plans, spec) == []


def test_status_table_escapes_pipes(tmp_path):
    plans, _ = _tree(tmp_path, STAGE1.replace("### Task D1: First", "### Task D1: a | b"))
    assert "| 1 | D1 | a \\| b | llm-dojo-scoring |" in R.render_status(R.parse_plans(plans))


def test_progress_before_dependency(tmp_path):
    """Ticking any step of a task whose dependency is incomplete is an error."""
    text = STAGE1.replace("- [x] **Step 2: b**", "- [ ] **Step 2: b**", 1)  # D1 1/2, D2 1/2
    plans, spec = _tree(tmp_path, text)
    R.write_status(plans)
    assert any("D2" in e and "D1" in e and "before" in e for e in R.check(plans, spec))
