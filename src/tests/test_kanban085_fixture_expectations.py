"""KANBAN-085 regression guards: FIXTURE_EXPECTATIONS must resolve to real files.

History: every key said ``tests/fixtures/…`` / ``examples/sources/…`` while the
repo-root-relative truth is ``src/tests/fixtures/…`` / ``docs/examples/sources/…``,
so ``_expectation_for()`` could never match and the intrinsic (doc_class,
subtype) expectations were silently skipped for every standalone fixture.
These network-free guards make silent rot impossible: every key must resolve,
every glob must match ≥1 fixture, and the matcher must actually engage.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "src" / "scripts" / "validate_pipeline.py"

_spec = importlib.util.spec_from_file_location("validate_pipeline_kanban085", _SCRIPT)
assert _spec is not None and _spec.loader is not None
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)


def test_no_stale_key_prefixes():
    """The original bug: keys pointed at pre-consolidation layout roots."""
    for key in vp.FIXTURE_EXPECTATIONS:
        assert not key.startswith("tests/fixtures/"), f"stale key: {key}"
        assert not key.startswith("examples/sources/"), f"stale key: {key}"


def test_every_literal_key_resolves_to_existing_file():
    for key, _ in vp.FIXTURE_EXPECTATIONS.items():
        if key.endswith("/*"):
            continue
        assert (REPO_ROOT / key).is_file(), f"expectation key not on disk: {key}"


def test_every_glob_matches_at_least_one_fixture():
    for key, _ in vp.FIXTURE_EXPECTATIONS.items():
        if not key.endswith("/*"):
            continue
        d = REPO_ROOT / key[:-2]
        assert d.is_dir(), f"glob directory missing: {key}"
        assert any(d.glob("*")), f"glob matches zero fixtures: {key}"


def test_matcher_engages_for_every_entry():
    """_expectation_for() returns each entry's expected class for a real path."""
    for key, (expected_cls, expected_subtype) in vp.FIXTURE_EXPECTATIONS.items():
        if key.endswith("/*"):
            d = REPO_ROOT / key[:-2]
            path = sorted(d.glob("*"))[0]
        else:
            path = REPO_ROOT / key
        cls, subtype, stage = vp._expectation_for(path, {})
        assert cls == expected_cls, f"{key}: got {cls!r}, want {expected_cls!r}"
        if expected_subtype is not None:
            assert subtype == expected_subtype
        assert stage is None  # intrinsic fixtures carry no stage expectation


def test_registry_shape_unchanged():
    """Pin the registry size so edits here are deliberate, not accidental."""
    assert len(vp.FIXTURE_EXPECTATIONS) == 12
