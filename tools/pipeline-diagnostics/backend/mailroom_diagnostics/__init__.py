"""Native pipeline diagnostics for llm-mailroom.

The first slice is read-only. It compiles the current workspace graph (or reads
an existing display contract) and reports structural findings. It never imports
``api.main``, never starts the watcher, never touches a network service and
never calls a model.
"""

__version__ = "0.1.0"
