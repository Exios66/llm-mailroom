#!/usr/bin/env python3
"""Generate the README artwork for llm-mailroom (constellation design v2).

    python3 docs/assets/readme/generate.py          # rewrite the SVGs beside this file
    python3 docs/assets/readme/generate.py --check  # read-only drift gate (exit 1 on drift)

`_art.py` is the shared kit (identical in every constellation repo); this file
owns only this repository's labels, scene and facts. Every number below is
backed by the README, `src/graph/build_graph.py` or `config/taxonomy.yaml`.
Standard library only, deterministic output.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _art as art  # noqa: E402  (shared kit lives beside this file)

HERO = {
    'title': 'The LLM-Mailroom',
    'kicker': 'THE MAILROOM CONSTELLATION · THE PIPELINE',
    'tagline': 'Ingest, classify, extract, verify — and archive every legal document with a hash-chained receipt.',
    'eyebrow': '13 GRAPH NODES · 5 DOCUMENT CLASSES · SQLITE-FIRST · FASTAPI + WATCHER',
    'caption': 'one LangGraph run per document · filesystem bins you can ls · every decision is a named node with a trace',
    'alt': 'Animated banner: envelopes ride a conveyor through intake, classify, extract, quality, compile and archive '
           'gates; a flagged envelope drops off the belt into a human-review bin.',
    'tagline_lines': ['Ingest, classify, extract, verify —', 'archive with a hash-chained receipt.'],
    'mobile_cards': [('Intake', 'claim · clean · prepare'), ('Classify', 'five classes + confidence'),
                     ('Extract', 'one specialist per class'), ('Archive', 'hash-chained audit log')],
    'mobile_note': 'one LangGraph run per document · 13 nodes',
}
GATES = [('INTAKE', art.SKY), ('CLASSIFY', art.GREEN), ('EXTRACT', art.LIME),
         ('QUALITY', art.VIOLET), ('COMPILE', art.ORANGE), ('ARCHIVE', art.PINK)]
STATS = [('13', 'graph nodes', art.SKY), ('5', 'document classes', art.VIOLET),
         ('SQLite', 'default catalog · no DB server', art.PINK), ('Hash chain', 'tamper-evident audit', art.GREEN),
         ('FastAPI', 'upload · status · audit', art.AMBER)]
STAGES = [('Intake', 'claim · transcribe · clean', 'manifest written'),
          ('Classify', 'SorterAgent · 5 classes', 'confidence-gated routing'),
          ('Extract', 'one specialist per class', 'schema + factuality guards'),
          ('Compile', 'matter record', 'procedural · no LLM call'),
          ('Catalog', 'SQLite documents + matters', 'write-catalog'),
          ('Archive', 'hash-chained audit log', 'manifest sidecar')]
SIDE = [('Retry', 'classify or extract again', 'bounded by retry_max'),
        ('Reviewer', 'second opinion on', 'medium-confidence classes'),
        ('Judge → Arbiter', 'ambiguous completeness:', 'stand · re-extract · human'),
        ('Boss', 'matter conflicts', 'adjudicated in-graph'),
        ('Human review', 'review bin → resolve', 'approve · reject · requeue')]
TERMINAL = [('cmd', 'git clone https://github.com/Exios66/llm-mailroom.git'),
            ('cmd', 'cd llm-mailroom && python3 -m venv .venv && source .venv/bin/activate'),
            ('cmd', 'python -m pip install -e ".[dev]"'),
            ('note', '# pipeline + API + tests · no database server needed'),
            ('cmd', 'python -m pytest src/tests/test_routing.py -q'),
            ('note', '# offline first check: no API key, no network'),
            ('cmd', 'PYTHONPATH=src python -m api.main'),
            ('note', '# FastAPI + embedded watcher drain the inbox bin'),
            ('cmd', 'curl http://localhost:8000/v1/health')]


TUI_GAUGES = [('graph nodes', '13', None, art.SKY), ('checkpoints', 'MemorySaver', None, art.VIOLET),
              ('catalog', 'SQLite first', None, art.GREEN), ('audit trail', 'hash chain', None, art.PINK),
              ('API', 'FastAPI', None, art.AMBER)]
TUI_LOG = [('ok', 'intake → classify → extract'), ('ok', 'quality → compile → archive'),
           ('warn', '0.88 ≤ confidence < 0.97 → review'), ('warn', 'unknown type → human review bin'),
           ('info', 'inbox → processing → archive|review')]
TUI_STATUS = 'one LangGraph run per document · every decision is a named node'


def build() -> dict[str, str]:
    """Return every generated asset keyed by file name."""
    flow_desc = ('Six main stages — intake, classify, extract, compile, catalog, archive — and the five exception '
                 'paths: retry, reviewer, judge and arbiter, boss, human review.')
    return {
        'hero.svg': art.hero(HERO, lambda: art.conveyor(GATES, y=292, branch=(3, 'HUMAN REVIEW'))),
        'hero-mobile.svg': art.hero(HERO, mobile=True),
        'stats.svg': art.stats(STATS, 'llm-mailroom by the numbers'),
        'stats-mobile.svg': art.stats(STATS, 'llm-mailroom by the numbers', mobile=True),
        'flow.svg': art.flow('One document, start to receipt', STAGES, SIDE, 'When it is not sure', desc=flow_desc),
        'flow-mobile.svg': art.flow('One document, start to receipt', STAGES, SIDE, 'When it is not sure',
                                    mobile=True, desc=flow_desc),
        'constellation.svg': art.constellation('llm-mailroom'),
        'constellation-mobile.svg': art.constellation('llm-mailroom', mobile=True),
        'tui.svg': art.tui('~/llm-mailroom — pipeline status', TUI_GAUGES, TUI_LOG, TUI_STATUS,
                           'Dashboard: the pipeline in facts (13 graph nodes, MemorySaver checkpoints, SQLite catalog, hash-chained audit trail, FastAPI) and the routing notes for documents that are not sure.', left='PIPELINE', right='ROUTING'),
        'terminal.svg': art.terminal('~/llm-mailroom — zsh', TERMINAL,
                                     'Animated terminal: clone, install, run the offline routing test, start the API '
                                     'and check its health endpoint.'),
    }


def main() -> None:
    """Write the assets, or compare them byte-for-byte with --check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='fail on drift without writing')
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    drift = []
    for name, content in build().items():
        path = base / name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != content:
                drift.append(name)
        else:
            path.write_text(content, encoding='utf-8')
    if drift:
        parser.exit(1, 'README asset drift: ' + ', '.join(drift) + '\n')
    print('README assets match' if args.check else 'README assets written')


if __name__ == '__main__':
    main()
