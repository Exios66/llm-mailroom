"""KANBAN-090 mirror guards: docclass variants in the mailroom repo.

Three contracts:
1. REGISTRY — one docclass key per classification-chain role (12 after
   court_opinion / due_diligence specialists were retired).
2. PURE APPEND — every variant startswith its live production template
   from prompt_templates() IN FULL; the base bytes are untouched and the
   docclass block rides after them.
3. PRODUCTION SAFETY — prompt_templates() is the agent-name-pinned
   production surface; docclass text reaches Langfuse only through the
   opt-in `--docclass` sync path under mailroom-docclass-<key> names.
   Production templates must not contain the docclass arm marker.
"""

from langchain_agents.prompts_docclass import (
    DOCCLASS_PROMPT_VERSIONS,
    _DOCCLASS_FROM_PRODUCTION,
)

EXPECTED_DOCCLASS_KEYS = 12


def _reg():
    return DOCCLASS_PROMPT_VERSIONS


def test_registry_complete():
    reg = _reg()
    assert len(reg) == EXPECTED_DOCCLASS_KEYS
    assert set(reg) == {
        "sorter_docclass_v0",
        "contracts_specialist_docclass_v0",
        "corporate_records_specialist_docclass_v0",
        "correspondence_specialist_docclass_v0",
        "compliance_specialist_docclass_v0",
        "insurance_claims_specialist_docclass_v0",
        "reviewer_docclass_v0",
        "arbiter_docclass_v0",
        "boss_docclass_v0",
        "judge_docclass_v0",
        "judge_classification_docclass_v0",
        "judge_correctness_docclass_v0",
    }
    for key, variant in reg.items():
        assert variant.strip(), f"empty variant: {key}"


def test_variants_are_pure_appends_of_production_templates():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    pairs = [
        (key, templates[agent_name])
        for agent_name, key, _body in _DOCCLASS_FROM_PRODUCTION
    ]
    assert len(pairs) == EXPECTED_DOCCLASS_KEYS
    for key, base in pairs:
        variant = _reg()[key]
        # FULL strict prefix — pure appendition of the live production text.
        # _append rstrips trailing newlines, so compare against the stripped
        # prefix; the production bytes themselves are otherwise untouched.
        assert variant.startswith(base.rstrip("\n")), f"base mutated or reordered: {key}"
        addition = variant[len(base.rstrip("\n")) :]
        assert len(addition) > 200, key
        assert "(KANBAN-090)" in addition, key
        assert "DOCCLASS ARM CONTEXT" in addition, key
        for cls in ("insurance_claim", "merger_agreement"):
            assert cls in addition, f"{cls} missing from {key}"
    contracts = _reg()["contracts_specialist_docclass_v0"]
    assert "CUAD families" in contracts
    assert "MAUD mergers" in contracts
    assert "cuad_clauses" in contracts
    assert "Anti-Assignment" in contracts
    assert "MAE Definition" in contracts
    assert "Type of Consideration" in contracts
    sorter = _reg()["sorter_docclass_v0"]
    assert "contract_subtype is null" in sorter
    assert "corporate_record even when" in sorter
    assert "CMS/Medicare claim tables" in sorter
    assert "Never emit unknown for readable" in sorter
    corporate = _reg()["corporate_records_specialist_docclass_v0"]
    assert "articles_of_incorporation" in corporate
    assert "rights_instrument" in corporate
    correspondence = _reg()["correspondence_specialist_docclass_v0"]
    assert "meeting_request" in correspondence
    assert "attorney_demand" in correspondence
    insurance = _reg()["insurance_claims_specialist_docclass_v0"]
    assert "pde" in insurance
    assert "DESYNPUF" in insurance
    compliance = _reg()["compliance_specialist_docclass_v0"]
    assert "form BODY" in compliance


def test_runtime_arm_rewrites_langchain_and_managed_lookups(monkeypatch):
    monkeypatch.delenv("MAILROOM_DOCCLASS_PROMPTS", raising=False)
    from langchain_agents.prompts import get_prompt
    from llm.prompts import get_managed_prompt, prompt_templates

    production = get_prompt("sorter_v14")
    assert "DOCCLASS ARM CONTEXT" not in production
    monkeypatch.setenv("MAILROOM_DOCCLASS_PROMPTS", "1")
    variant = get_prompt("sorter_v14")
    assert variant.startswith(production.rstrip("\n"))
    assert "DOCCLASS ARM CONTEXT" in variant
    assert "merger_agreement" in variant
    text, _obj = get_managed_prompt("sorter_reviewer", "LOCAL FALLBACK")
    assert "DOCCLASS ARM CONTEXT" in text
    # Production catalog must stay the un-rewritten templates even while
    # the runtime arm is on (Langfuse sync must never overwrite mailroom-sorter).
    assert all("DOCCLASS ARM CONTEXT" not in t for t in prompt_templates().values())
    monkeypatch.delenv("MAILROOM_DOCCLASS_PROMPTS", raising=False)


def test_production_surface_has_no_docclass_arm():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    assert len(templates) == 14
    assert not any(key.endswith("_docclass_v0") for key in templates)
    assert all("DOCCLASS ARM CONTEXT" not in t for t in templates.values())
    # Supporting agents that are production-only (not a docclass role).
    assert "reporter" in templates and "pdf_transcriber" in templates


def test_sync_docclass_path_is_opt_in_and_namespaced():
    reg = _reg()
    try:
        from scripts.sync_prompts import sync_one
    except Exception:
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "sync_prompts.py"
        ).read_text()
        assert '"--docclass"' in script
        assert 'f"docclass-{key}"' in script
        return
    for key in ("boss_docclass_v0", "judge_classification_docclass_v0", "sorter_docclass_v0"):
        status = sync_one(
            None, f"docclass-{key}", reg[key], force=False, dry_run=True
        )
        assert status.startswith("create")
        assert "mailroom-docclass-" in status
        assert status.rstrip().endswith(key)
