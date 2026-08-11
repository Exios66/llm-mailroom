"""Per-agent SKILL FILES for the vendored LangChain agents.

Each designated agent gets its own reference material — domain knowledge that
lives OUTSIDE the eval-validated prompt text so the versioned prompts stay
byte-stable (issue #10) while the agent's working context can grow.

Layout: ``langchain_agents/skills/<agent_name>/*.md`` — one file per topic.
Every file in an agent's skills dir is appended to its system prompt as a
``## Skill reference: <topic>`` section (below the base prompt), so the agent
can use the knowledge without changing the eval-validated core.

The sorter ships with:
  - ``cuad-subtypes.md`` — the 25 CUAD agreement families, the paper's folder
    taxonomy, and the equivalence classes (reseller<->distributor, ...) that
    the subtype evaluation historically confused.
  - ``confidence-calibration.md`` — when to be decisive vs flag for review.

The contracts specialist ships with:
  - ``contract-clauses.md`` — the 41 CUAD clause categories and the extraction
    schema's field expectations per agreement family.

Adding a skill = dropping a ``.md`` file in the agent's directory; the
``load_skills()`` loader picks it up at process start (no code change).
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_HEADER_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def load_skills(agent_name: str, max_chars: int = 6000) -> str:
    """Return the agent's skill files as an appended prompt section.

    ``max_chars`` bounds the total injected context so skill files can never
    blow the input budget; files are loaded newest-first (lexicographic
    reverse) so later additions win the budget.
    """
    agent_dir = SKILLS_DIR / agent_name
    if not agent_dir.exists():
        return ""
    sections: list[str] = []
    used = 0
    for path in sorted(agent_dir.glob("*.md"), reverse=True):
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if not text:
            continue
        title = "skill"
        m = _HEADER_RE.search(text)
        if m:
            title = m.group(1).strip().lower().replace(" ", "-")
        section = f"## Skill reference: {title}\n\n{text}"
        if used + len(section) > max_chars:
            budget = max_chars - used
            if budget > 400:
                sections.append(section[:budget] + "\n[... truncated ...]")
            break
        sections.append(section)
        used += len(section)
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)
