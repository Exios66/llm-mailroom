"""The affected-test selector maps changed files to the tests that see them."""

from scripts.affected_tests import select


def test_module_change_selects_its_direct_tests_only():
    tests = select(["src/pipeline/gmail_intake.py"])
    assert "src/tests/test_gmail_intake.py" in tests
    assert "src/tests/test_landing.py" not in tests


def test_depth_widens_to_tests_of_importers():
    narrow = set(select(["src/pipeline/gmail_intake.py"], depth=0))
    wide = set(select(["src/pipeline/gmail_intake.py"], depth=-1))
    assert narrow < wide


def test_changed_test_file_selects_itself_and_literal_path_consumers():
    assert set(select(["src/tests/test_run_limits.py"])) == {
        "src/tests/test_run_limits.py", "src/tests/test_affected_tests.py",
    }


def test_data_file_selects_tests_naming_it():
    assert "src/tests/test_docs_truth.py" in select(["CHANGELOG.md"])


def test_conftest_change_selects_everything():
    assert len(select(["src/tests/conftest.py"])) > 50


def test_subprocess_mode_guards_are_selected_for_cli_changes():
    for path in ("src/scripts/run_quality_judges.py", "src/scripts/run_pilot.py"):
        assert "src/tests/test_mock_isolation.py" in select([path], depth=-1)
