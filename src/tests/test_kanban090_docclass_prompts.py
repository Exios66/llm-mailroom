"""KANBAN-090 mirror guards: docclass variants in the mailroom repo.

Three contracts:
1. REGISTRY — one docclass key per classification-chain role (14: the
   original 13 plus sorter, now that sorter has a production-ready base).
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

EXPECTED_DOCCLASS_KEYS = 14


def _reg():
    return DOCCLASS_PROMPT_VERSIONS


def test_registry_complete():
    reg = _reg()
    assert len(reg) == EXPECTED_DOCCLASS_KEYS
    assert set(reg) == {
        "sorter_docclass_v0",
        "contracts_specialist_docclass_v0",
        "corporate_records_specialist_docclass_v0",
        "due_diligence_specialist_docclass_v0",
        "correspondence_specialist_docclass_v0",
        "compliance_specialist_docclass_v0",
        "court_opinions_specialist_docclass_v0",
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


def test_production_surface_has_no_docclass_arm():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    assert len(templates) == 16
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
