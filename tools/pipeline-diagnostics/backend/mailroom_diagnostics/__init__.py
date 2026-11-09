"""Native pipeline diagnostics for llm-mailroom.

The implemented slice is read-only. It compiles the current workspace graph
(or reads an existing display contract) and reports structural findings. It
never imports ``api.main``, never starts the watcher and never calls a model.
``serve`` optionally exposes the same data over loopback HTTP; it makes no
outbound network call and requires a bearer token for off-loopback binds.
"""

__version__ = "0.1.0"
