"""Relations scanner CLI (HUB-040/HUB-051) — the documented entrypoint.

``PYTHONPATH=src python -m pipeline.relations_scan [--full] [--doc DOC_ID]
[--verify-ledger]``

HUB-051 audit gap: the module was referenced by docs and the board card but
never created — ``main()`` sat stranded in ``pipeline/relations.py`` and the
command crashed with ``ModuleNotFoundError``. This thin module is the
documented name.
"""

from .relations import main

if __name__ == "__main__":
    raise SystemExit(main())