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
    assert "Hermes the owl" in html
    assert "Hoot" not in html
    assert "assets/mascot/fumi-icon.png" not in html
    copy_js = html.split("const text = ", 1)[1].split(";", 1)[0]
    assert "git clone https://github.com/Exios66/llm-mailroom.git" in copy_js
    assert "cd llm-mailroom" in copy_js
    assert 'pip install -e ".[dev]"' in copy_js
    assert "PYTHONPATH=src python -m api.main" in copy_js


def test_gitbook_home_ports_the_enhanced_landing():
    home = GITBOOK_HOME.read_text(encoding="utf-8")
    assert home.lstrip().startswith("<table")
    assert "# The LLM-Mailroom" in home
    assert "fumi.gif" in home
    assert home.index("banner.png") < home.index("fumi.gif")
    assert "banner.png" in home
    assert "A multi-agent pipeline that ingests, classifies, extracts, and archives" in home
    home_encoded = home.replace("|", "%7C")
    for badge in BADGES:
        assert badge in home_encoded
    assert home.index(BADGES[-1]) < home.index("banner.png")
    assert "night-shift owl at the sorting desk" in home.split("banner.png", 1)[1]
    assert INSTALL in home
    assert "overview.md" in home
    assert "From inbox to archive" in home
    assert "Meet Fumi" in home
    assert 'Postal Worker Fumi (文, "letter") on duty' in home
    assert "Read the docs" in home
    assert "architecture.md" in home
    assert "**release** · v0.7.1" in home
    assert "hoot-icon.png" in home
    assert "Pixelify" not in home
    assert "font-family" not in home
    assert "fonts.googleapis.com" not in home
    # Header is a centered The LLM-Mailroom wordmark — no Fumi before the masthead.
    assert home.lstrip().startswith("<table")
    header = home.split("banner.png", 1)[0]
    assert 'width="100%"' in header
    assert 'align="center"' in header
    assert "# The LLM-Mailroom" in header
    assert "fumi.gif" not in header
    assert 'Fumi (文, "letter")' not in header
    assert "lives in this header corner" not in header
    assert "not the header itself" not in header
    # Name + 文 live on the on-duty Fumi, after the banner.
    on_duty = home.split("banner.png", 1)[1].split("## From inbox to archive", 1)[0]
    assert "fumi.gif" in on_duty
    assert 'Postal Worker Fumi (文, "letter") on duty' in on_duty
    assert "Specialist agents on a 13-node graph" in on_duty
    meet = home.split("## Meet Fumi", 1)[1]
    assert "Hermes" in meet
    assert "Hoot" not in meet
    assert "standalone static page uses this icon" not in meet
    assert "GitBook's published tab icon" not in meet
    assert "upload this same file" not in meet
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

    # GitBook publishes the URL-mapped copy, not docs/constellation/maintaining.md.
    published_maintaining = (REPO / "docs" / "about-this-site" / "maintaining.md")
    if published_maintaining.is_file():
        maintaining = published_maintaining.read_text(encoding="utf-8")
    else:
        maintaining = (REPO / "docs" / "constellation" / "maintaining.md").read_text(
            encoding="utf-8"
        )
    assert "centered **The LLM-Mailroom** wordmark" in maintaining
    assert "Postal Worker Fumi" in maintaining
    assert "header corner" not in maintaining
    assert "Hoot" not in maintaining
    assert "Hermes" in maintaining

    # GitBook's Project directory is docs/; GITBOOK-SITE writes this file there.
    # A later export may wrap the space in a section and add a changelog space.
    site_path = REPO / "docs" / "gitbook-docs.yaml"
    assert site_path.is_file()
    site_cfg = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    assert site_cfg["site"]["title"] == "Mailroom Inc. Docs"

    def _mailroom_space(node):
        if isinstance(node, dict):
            if node.get("key") == "mailroom-docs":
                return node
            for child in node.get("children") or node.get("structure") or []:
                found = _mailroom_space(child)
                if found is not None:
                    return found
        if isinstance(node, list):
            for child in node:
                found = _mailroom_space(child)
                if found is not None:
                    return found
        return None

    space = _mailroom_space(site_cfg["site"]["structure"])
    assert space is not None
    assert space["content"]["directory"] == "./"
    assert space["default"] is True
    # Repo-root fallback if the Git Sync Project directory is ever moved to root.
    root_site = REPO / "gitbook-docs.yaml"
    if root_site.is_file():
        root_cfg = yaml.safe_load(root_site.read_text(encoding="utf-8"))
        root_space = _mailroom_space(root_cfg["site"]["structure"])
        assert root_space is not None
        assert root_space["content"]["directory"] in {"./", "./docs"}
        assert root_cfg["site"]["title"] == "Mailroom Inc. Docs"
    assert "https://mailroom-inc.gitbook.io/mailroom-inc.-docs/" in home
    space_cfg = (REPO / "docs" / ".gitbook.yaml").read_text(encoding="utf-8")
    assert "readme: README.md" in space_cfg
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for badge in BADGES:
        assert badge in readme
    assert "https://github.com/grantmooslin" in home
    assert "](https://github.com/LLM-Mailroom-Services)" in home
    assert "https://github.com/grantmooslin" in readme
    assert "](https://github.com/LLM-Mailroom-Services)" in readme
