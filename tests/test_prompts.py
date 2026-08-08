import os

import pytest


class TestPromptFallback:
    def test_renders_local_template_without_langfuse(self, monkeypatch):
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        monkeypatch.setattr("llm.prompts._prompt_cache", {})
        from llm.prompts import get_managed_prompt

        text, obj = get_managed_prompt("sorter", "Classes:\n{{doc_type_descriptions}}", {"doc_type_descriptions": "A, B"})
        assert "Classes:\nA, B" == text
        assert obj is None

    def test_renders_local_template_without_variables(self, monkeypatch):
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        monkeypatch.setattr("llm.prompts._prompt_cache", {})
        from llm.prompts import get_managed_prompt

        text, obj = get_managed_prompt("contracts_specialist", "Static prompt text.")
        assert text == "Static prompt text."
        assert obj is None


class TestPromptTemplates:
    def test_all_agents_have_templates(self):
        from llm.prompts import prompt_templates

        templates = prompt_templates()
        assert len(templates) == 11
        for agent in (
            "sorter",
            "contracts_specialist",
            "corporate_records_specialist",
            "due_diligence_specialist",
            "correspondence_specialist",
            "compliance_specialist",
            "court_opinions_specialist",
            "boss",
            "reporter",
            "pdf_transcriber",
            "judge",
        ):
            assert agent in templates
            assert templates[agent].strip()

    def test_sorter_uses_variable_placeholder(self):
        from llm.prompts import prompt_templates

        assert "{{doc_type_descriptions}}" in prompt_templates()["sorter"]

    def test_static_templates_have_no_unresolved_variables(self):
        from llm.prompts import prompt_templates

        templates = prompt_templates()
        for agent, template in templates.items():
            if agent == "sorter":
                continue
            assert "{{" not in template, f"{agent} template has unresolved variable"
