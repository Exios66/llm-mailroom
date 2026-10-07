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


def test_changed_test_file_selects_itself():
    assert select(["src/tests/test_run_limits.py"]) == ["src/tests/test_run_limits.py"]


def test_data_file_selects_tests_naming_it():
    assert "src/tests/test_docs_truth.py" in select(["CHANGELOG.md"])


def test_conftest_change_selects_everything():
    assert len(select(["src/tests/conftest.py"])) > 50
