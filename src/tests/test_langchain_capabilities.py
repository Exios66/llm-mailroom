"""Per-agent skills + tools + outcome memory for vendored LangChain agents.

Covers langchain_agents/skills.py, toolkit.py, memory.py, and the
BaseAgent.augmented_system_prompt() hook (skills appended below the
eval-validated prompt head, per-agent tools, and retry-facing memory).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
sys.path.insert(0, str(REPO_ROOT / "src"))


class TestSkills:
    def test_sorter_has_skill_files(self):
        from langchain_agents.skills import load_skills, SKILLS_DIR

        assert (SKILLS_DIR / "sorter").exists()
        ctx = load_skills("sorter")
        assert "CUAD" in ctx or "equivalence" in ctx
        assert "## Skill reference" in ctx

    def test_contracts_specialist_has_skill_files(self):
        from langchain_agents.skills import load_skills

        ctx = load_skills("contracts_specialist")
        assert "clause" in ctx.lower() or "termination" in ctx.lower()

    def test_unknown_agent_no_skills(self):
        from langchain_agents.skills import load_skills

        assert load_skills("not_an_agent") == ""

    def test_boss_and_reviewer_have_skill_files(self):
        from langchain_agents.skills import load_skills

        assert "approved" in load_skills("boss").lower() or "review" in load_skills("boss").lower()
        assert "independent" in load_skills("sorter_reviewer").lower()

    def test_sorter_confidence_calibration_skill(self):
        from langchain_agents.skills import load_skills

        ctx = load_skills("sorter")
        assert "confidence" in ctx.lower()

    def test_budget_bound(self):
        from langchain_agents.skills import load_skills

        tiny = load_skills("sorter", max_chars=200)
        assert len(tiny) <= 200


class TestToolkit:
    def test_sorter_toolkit_has_classification_tools(self):
        from langchain_agents.toolkit import get_tools, render_tools

        names = {t.name for t in get_tools("sorter")}
        assert "taxonomy" in names
        assert "contract_subtypes" in names
        assert "memory" in names
        assert "## Available tools" in render_tools("sorter")

    def test_contracts_toolkit_has_schema_tools(self):
        from langchain_agents.toolkit import get_tools

        names = {t.name for t in get_tools("contracts_specialist")}
        assert "extraction_schema" in names
        assert "field_types" in names

    def test_tool_run_never_raises(self):
        from langchain_agents.toolkit import get_tools

        for t in get_tools("sorter"):
            result = t.run()
            assert result is not None


class TestMemory:
    def test_record_and_recent_context(self, temp_base_dir, monkeypatch):
        monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
        from langchain_agents import memory

        assert memory.record_outcome(
            "sorter", doc_type="contract", decision="contract/license",
            confidence=0.5, feedback="guardrail rejected bad subtype",
            source="guardrail",
        )
        assert memory.record_outcome(
            "sorter", doc_type="contract", decision="contract/distributor",
            confidence=0.9, feedback="clean run",
            source="run",
        )
        ctx = memory.recent_context("sorter", "contract", k=5)
        assert "guardrail rejected" in ctx
        assert "clean run" in ctx
        s = memory.stats("sorter")
        assert s["total"] == 2
        assert s["by_source"]["guardrail"] == 1

    def test_recent_context_empty_for_unknown_agent(self, temp_base_dir, monkeypatch):
        monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
        from langchain_agents.memory import recent_context

        assert recent_context("boss", "contract") == ""

    def test_corrupt_line_skipped(self, temp_base_dir, monkeypatch):
        monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
        from langchain_agents import memory
        from pathlib import Path

        p = memory._memory_path("sorter")
        p.write_text('{"ts": 1, "agent": "sorter", "doc_type": "contract", "decision": "a", "confidence": 0.5, "feedback": "ok", "source": "run"}\nNOT JSON\n')
        ctx = memory.recent_context("sorter", "contract", k=5)
        assert "ok" in ctx  # valid line still read; corrupt line skipped


class TestAugmentedPrompt:
    def test_augmented_prompt_appends_skills_and_tools(self, monkeypatch):
        from langchain_agents.base_agent import BaseAgent

        class A(BaseAgent):
            agent_name = "sorter"

            def system_prompt(self) -> str:
                return "BASE PROMPT"

        agent = A(model="qwen/qwen3.7-flash")
        full = agent.augmented_system_prompt(doc_type="contract")
        assert full.startswith("BASE PROMPT")          # eval-validated head preserved
        assert "## Skill reference" in full            # skills appended
        assert "## Available tools" in full            # tools appended
