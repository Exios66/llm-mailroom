"""Run landing-page JavaScript unit tests through the repository's pytest entry point."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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
    "contributor-grantmooslin-blue",
    "org-LLM--Mailroom--Services-24292F",
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
    assert "https://github.com/grantmooslin" in html
    assert 'href="https://github.com/LLM-Mailroom-Services"' in html
    assert 'role="group"' in html
    assert 'role="img"' not in html.split("id=\"floor\"", 1)[1][:800]
    assert 'media="(prefers-reduced-motion: reduce)"' in html
    assert 'srcset="assets/mascot/fumi.png"' in html
    assert INSTALL in html
    assert 'rel="icon"' in html
    assert 'href="assets/mascot/hoot-icon.png"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "assets/mascot/fumi-icon.png" not in html
    copy_js = html.split("const text = ", 1)[1].split(";", 1)[0]
    assert "git clone https://github.com/Exios66/llm-mailroom.git" in copy_js
    assert "cd llm-mailroom" in copy_js
    assert 'pip install -e ".[dev]"' in copy_js
    assert "PYTHONPATH=src python -m api.main" in copy_js


def _mailroom_docs_space(cfg: dict) -> dict:
    """GitBook export may nest the space under a section."""
    for item in cfg["site"]["structure"]:
        if item.get("key") == "mailroom-docs":
            return item
        for child in item.get("children") or []:
            if child.get("key") == "mailroom-docs":
                return child
    raise AssertionError("mailroom-docs space missing from gitbook-docs.yaml")


def test_gitbook_home_ports_the_enhanced_landing():
    home = GITBOOK_HOME.read_text(encoding="utf-8")
    assert home.startswith("# The LLM-Mailroom\n")
    assert "fumi.gif" in home
    assert "banner.png" in home
    assert home.index("banner.png") < home.index("fumi.gif")
    assert "A multi-agent pipeline that ingests, classifies, extracts, and archives" in home
    for badge in BADGES:
        assert badge in home
    assert home.index(BADGES[-1]) < home.index("banner.png")
    assert "night-shift owl at the sorting desk" in home.split("banner.png", 1)[1]
    assert INSTALL in home
    assert "From inbox to archive" in home
    assert "Meet Fumi" in home
    assert "Postal maid on duty" in home
    assert "Read the docs" in home
    assert "architecture.md" in home
    assert "**release** · v0.7.1" in home
    assert "hoot-icon.png" in home
    assert "Pixelify" not in home
    assert "font-family" not in home
    assert "fonts.googleapis.com" not in home
    # Header is the wordmark + 文 caption only — Fumi's sprite is below the banner.
    assert home.strip().startswith("# The LLM-Mailroom")
    header = home.split("# The LLM-Mailroom", 1)[1].split("banner.png", 1)[0]
    assert "fumi.gif" not in header
    assert "<img" not in header
    assert "<table" not in header
    assert 'Fumi (文, "letter")' in header
    assert "lives in this header corner" not in header
    assert "not the header itself" not in header
    meet = home.split("## Meet Fumi", 1)[1]
    assert "build_mascot.py" not in meet
    assert "header corner" not in meet
    assert "landing/" not in meet.split("## Related files", 1)[0]
    summary = (REPO / "docs" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "* [The LLM-Mailroom](README.md)" in summary
    assert "docker-deployment.md" in summary
    assert "modal-vllm.md" in summary
    assert "local-mailroom-sandbox-reports.md" in summary
    assert "local-mailroom-sandbox-visuals.md" in summary
    assert "docker-deployment.md" in home
    assert "modal-vllm.md" in home

    # GitBook's Project directory is docs/; GITBOOK-SITE writes this file there.
    site_path = REPO / "docs" / "gitbook-docs.yaml"
    assert site_path.is_file()
    site_cfg = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    space = _mailroom_docs_space(site_cfg)
    assert space["key"] == "mailroom-docs"
    assert space["content"]["directory"] == "./"
    assert space["default"] is True
    assert site_cfg["site"]["title"] == "Mailroom Inc. Docs"
    # Repo-root fallback if the Git Sync Project directory is ever moved to root.
    root_site = REPO / "gitbook-docs.yaml"
    if root_site.is_file():
        root_cfg = yaml.safe_load(root_site.read_text(encoding="utf-8"))
        root_space = _mailroom_docs_space(root_cfg)
        assert root_space["key"] == "mailroom-docs"
        assert root_space["content"]["directory"] == "./docs"
        assert root_cfg["site"]["title"] == "Mailroom Inc. Docs"
    assert "https://mailroom-inc.gitbook.io/mailroom-inc.-docs/" in home
    space = (REPO / "docs" / ".gitbook.yaml").read_text(encoding="utf-8")
    assert "readme: README.md" in space
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for badge in BADGES:
        assert badge in readme
    assert "https://github.com/grantmooslin" in home
    assert "](https://github.com/LLM-Mailroom-Services)" in home
    assert "https://github.com/grantmooslin" in readme
    assert "](https://github.com/LLM-Mailroom-Services)" in readme
