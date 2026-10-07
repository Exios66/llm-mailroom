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
        assert len(templates) == 18  # 16 agents + judge-classification/correctness + relations (HUB-040)
        for agent in (
            "sorter",
            "sorter_reviewer",
            "contracts_specialist",
            "merger_agreement_specialist",
            "corporate_records_specialist",
            "correspondence_specialist",
            "insurance_claims_specialist",
            "boss",
            "reporter",
            "pdf_transcriber",
            "image_extractor",
            "judge",
            "judge-classification",
            "judge-correctness",
            "arbiter",
            "gmail_triage",
            "intake",
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


class _FakePrompt:
    prompt = "managed text"

    def compile(self, **kw):
        return "managed text"


class _FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def get_prompt(self, name, label=None):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _wire(monkeypatch, client, clock=None):
    monkeypatch.setattr("llm.prompts._prompt_cache", {})
    monkeypatch.setattr("llm.prompts._client", lambda: client)
    if clock is not None:
        monkeypatch.setattr("llm.prompts._now", clock)


class TestPromptCacheExpiry:
    def test_none_result_is_not_cached(self, monkeypatch):
        """A transient fetch failure must not pin the local fallback."""
        client = _FakeClient([RuntimeError("503"), _FakePrompt()])
        _wire(monkeypatch, client)
        from llm.prompts import get_managed_prompt

        text1, obj1 = get_managed_prompt("sorter", "local")
        assert (text1, obj1) == ("local", None)
        text2, obj2 = get_managed_prompt("sorter", "local")
        assert text2 == "managed text" and obj2 is not None
        assert client.calls == 2

    def test_cache_entry_expires_after_ttl(self, monkeypatch):
        t = {"now": 1000.0}
        client = _FakeClient([_FakePrompt(), _FakePrompt()])
        _wire(monkeypatch, client, clock=lambda: t["now"])
        monkeypatch.setenv("MAILROOM_PROMPT_CACHE_TTL", "60")
        from llm.prompts import get_managed_prompt

        get_managed_prompt("sorter", "local")
        t["now"] += 59
        get_managed_prompt("sorter", "local")
        assert client.calls == 1
        t["now"] += 2
        get_managed_prompt("sorter", "local")
        assert client.calls == 2

    def test_default_ttl_is_60_seconds(self):
        from llm.prompts import prompt_cache_ttl

        import os

        os.environ.pop("MAILROOM_PROMPT_CACHE_TTL", None)
        assert prompt_cache_ttl() == 60.0
