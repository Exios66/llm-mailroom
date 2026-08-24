"""KANBAN-090 mirror guards: docclass variants in the mailroom repo.

Three contracts:
1. REGISTRY — 13 docclass keys, one per classification-chain role.
2. PURE APPEND — every variant startswith its real production base IN FULL;
   the base bytes are untouched and the docclass block rides after them.
3. PRODUCTION SAFETY — prompt_templates() stays exactly the thirteen
   agent-name-pinned templates; docclass text reaches Langfuse only through
   the opt-in `--docclass` sync path under mailroom-docclass-<key> names.
"""

EXPECTED_DOCCLASS_KEYS = 13


def _reg():
    from langchain_agents.prompts_docclass import DOCCLASS_PROMPT_VERSIONS

    return DOCCLASS_PROMPT_VERSIONS


def test_registry_complete():
    reg = _reg()
    assert len(reg) == EXPECTED_DOCCLASS_KEYS
    assert set(reg) == {
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


def test_variants_are_pure_appends_of_real_bases():
    from agents import (
        arbiter,
        boss,
        insurance_claims_specialist,
        judge,
        sorter_reviewer,
    )
    from langchain_agents import prompts as LP

    pairs = [
        ("contracts_specialist_docclass_v0", LP.CONTRACTS_SPECIALIST_PROMPT),
        ("corporate_records_specialist_docclass_v0", LP.CORPORATE_RECORDS_SPECIALIST_PROMPT),
        ("due_diligence_specialist_docclass_v0", LP.DUE_DILIGENCE_SPECIALIST_PROMPT),
        ("correspondence_specialist_docclass_v0", LP.CORRESPONDENCE_SPECIALIST_PROMPT),
        ("compliance_specialist_docclass_v0", LP.COMPLIANCE_SPECIALIST_PROMPT),
        ("court_opinions_specialist_docclass_v0", LP.COURT_OPINIONS_SPECIALIST_PROMPT),
        (
            "insurance_claims_specialist_docclass_v0",
            insurance_claims_specialist.SYSTEM_PROMPT,
        ),
        ("reviewer_docclass_v0", sorter_reviewer.REVIEWER_SYSTEM_PROMPT),
        ("arbiter_docclass_v0", arbiter.ARBITER_SYSTEM_PROMPT),
        ("boss_docclass_v0", boss.BOSS_SYSTEM_PROMPT),
        ("judge_docclass_v0", judge.SYSTEM_PROMPT),
        ("judge_classification_docclass_v0", judge.CLASSIFICATION_SYSTEM_PROMPT),
        ("judge_correctness_docclass_v0", judge.CORRECTNESS_SYSTEM_PROMPT),
    ]
    assert len(pairs) == EXPECTED_DOCCLASS_KEYS
    for key, base in pairs:
        variant = _reg()[key]
        # FULL strict prefix — pure appendition, base bytes byte-identical.
        assert variant.startswith(base), f"base mutated or reordered: {key}"
        addition = variant[len(base) :]
        assert len(addition) > 200, key
        assert "(KANBAN-090)" in addition, key
        assert "DOCCLASS ARM CONTEXT" in addition, key
        # The extended class set travels in every addition.
        for cls in ("insurance_claim", "merger_agreement"):
            assert cls in addition, f"{cls} missing from {key}"


def test_production_surface_untouched():
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    assert len(templates) == 13  # pre-existing pin, must not grow
    assert not any(key.endswith("_docclass_v0") for key in templates)
    assert all("DOCCLASS ARM CONTEXT" not in t for t in templates.values())


def test_sync_docclass_path_is_opt_in_and_namespaced():
    reg = _reg()
    try:
        from scripts.sync_prompts import sync_one
    except Exception:
        # Env bootstrap unavailable here: verify the wiring textually instead.
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "sync_prompts.py"
        ).read_text()
        assert '"--docclass"' in script
        assert 'f"docclass-{key}"' in script
        return
    # Dry run never touches the client: safe to pass None.
    for key in ("boss_docclass_v0", "judge_classification_docclass_v0"):
        status = sync_one(
            None, f"docclass-{key}", reg[key], force=False, dry_run=True
        )
        # Namespaced target: mailroom-docclass-<key> — never an agent name.
        assert status.startswith("create")
        assert "mailroom-docclass-" in status
        assert status.rstrip().endswith(key)
