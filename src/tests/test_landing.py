"""Run landing-page JavaScript unit tests through the repository's pytest entry point."""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.bump_dojo_scoring import current_pin

REPO = Path(__file__).resolve().parents[2]
LANDING = REPO / "landing" / "index.html"
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
    f"dojo-{current_pin(REPO)}-6f42c1",
    "release-v0.8.0-2EA043",
    "contributor-Exios66-blue",
    "contributor-grantmooslin-blue",
    "org-LLM--Mailroom--Services-24292F",
)


@pytest.mark.parametrize("relative_path", ["README.md", "landing/index.html"])
def test_dojo_badge_links_to_the_pinned_release(relative_path):
    """The badge, release destination, and HTML alt text must agree with the pin."""
    tag = current_pin(REPO)
    badge = f"https://img.shields.io/badge/dojo-{tag}-6f42c1"
    release = f"https://github.com/Exios66/llm-dojo-scoring/releases/tag/{tag}"
    text = (REPO / relative_path).read_text(encoding="utf-8")
    if relative_path.endswith(".html"):
        assert (
            f'<a href="{release}"><img src="{badge}" '
            f'alt="llm-dojo-scoring {tag}"></a>'
        ) in text
    else:
        assert f"[![Dojo]({badge})]({release})" in text
    assert text.count("https://img.shields.io/badge/dojo-") == 1


@pytest.mark.parametrize("path", ["README.md", "docs/README.md", "landing/index.html"])
def test_release_badge_label_and_destination_match_package(path):
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = f"v{project['version']}"
    text = (REPO / path).read_text(encoding="utf-8")
    badge = f"https://img.shields.io/badge/release-{version}-2EA043"
    destination = f"https://github.com/LLM-Mailroom-Services/Digital-Mailroom/releases/tag/{version}"
    if path.endswith(".html"):
        badges = re.findall(
            r'<a\s+href="([^"]+)"[^>]*>\s*<img\s+src="(https://img.shields.io/badge/release-[^"]+)"\s+alt="([^"]+)"[^>]*>\s*</a>',
            text,
        )
        assert badges == [(destination, badge, f"Release {version}")]
        tags = re.findall(r'<em>release</em>\s*·\s*(v[\d.]+)', text)
        assert tags == [version]
    else:
        badges = re.findall(r"\[!\[Release\]\(([^)]+)\)\]\(([^)]+)\)", text)
        assert badges == [(badge, destination)]
        if path == "README.md":
            assert f"[`{version}`]({destination})" in text
        else:
            tags = re.findall(r"\[\*\*release\*\* · (v[\d.]+)\]", text)
            assert tags == [version]


def _walk_gitbook_nodes(nodes):
    for node in nodes:
        yield node
        yield from _walk_gitbook_nodes(node.get("children") or [])


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


