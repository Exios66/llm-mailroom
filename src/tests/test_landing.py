"""Run landing-page JavaScript unit tests through the repository's pytest entry point."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LANDING = REPO / "landing" / "index.html"
GITBOOK_HOME = REPO / "docs" / "README.md"
INSTALL = (
    'git clone https://github.com/Exios66/llm-mailroom.git\n'
    'cd llm-mailroom\n'
    'pip install -e ".[dev]"\n'
    'PYTHONPATH=src python -m api.main'
)
BADGES = (
    "python-3.11%2B-blue",
    "LangGraph-13--node%20state%20machine-4C8CBF",
    "LLM-OpenRouter%20%7C%20Ollama%20%7C%20vLLM-8A2BE2",
    "tracing-Langfuse%20%7C%20Braintrust%20%7C%20Phoenix-F5A623",
    "storage-SQLite--first-lightgrey",
    "release-v0.7.1-2EA043",
    "contributor-Exios66-blue",
)


def test_landing_javascript_behaviors():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Landing-page behavioral tests require Node.js >=18")
    tests = Path(__file__).with_suffix(".test.cjs")
    result = subprocess.run(
        [node, "--test", str(tests)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_landing_html_header_masthead_and_coderabbit_contracts():
    html = LANDING.read_text(encoding="utf-8")
    assert 'class="fumi-mark"' in html
    assert 'src="assets/mascot/fumi.svg"' in html
    assert 'The <b>LLM</b>-Mailroom' in html
    assert html.index('class="fumi-mark"') < html.index('class="masthead"')
    assert 'src="assets/banner.png"' in html
    assert "<h1>The <span class=\"llm\">LLM</span>-Mailroom</h1>" in html
    for badge in BADGES:
        assert badge in html
    assert 'role="group"' in html
    assert 'role="img"' not in html.split("id=\"floor\"", 1)[1][:800]
    assert 'media="(prefers-reduced-motion: reduce)"' in html
    assert 'srcset="assets/mascot/fumi.png"' in html
    assert INSTALL in html
    assert INSTALL in html.split("const text = ", 1)[1]


def test_gitbook_home_ports_the_enhanced_landing():
    home = GITBOOK_HOME.read_text(encoding="utf-8")
    assert home.startswith("# The LLM-Mailroom\n")
    assert 'src="assets/fumi/fumi.gif"' in home
    assert home.index("assets/fumi/fumi.gif") < home.index("assets/banner.png")
    assert 'src="assets/banner.png"' in home
    assert "A multi-agent pipeline that ingests, classifies, extracts, and archives" in home
    for badge in BADGES:
        assert badge in home
    assert INSTALL in home
    assert "constellation/overview.md" in home
    assert "From inbox to archive" in home
    assert "Meet Fumi" in home
    # Fumi is in the header table, not the sole opening figure.
    assert home.strip().startswith("# The LLM-Mailroom")
    assert "<table>" in home.split("# The LLM-Mailroom", 1)[1].split("assets/banner.png", 1)[0]
    summary = (REPO / "docs" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "* [The LLM-Mailroom](README.md)" in summary
    site = (REPO / "gitbook-docs.yaml").read_text(encoding="utf-8")
    assert "key: mailroom-docs" in site
    assert "path: /" in site
    assert "directory: ./docs" in site
    assert "default: true" in site
    space = (REPO / "docs" / ".gitbook.yaml").read_text(encoding="utf-8")
    assert "readme: README.md" in space
