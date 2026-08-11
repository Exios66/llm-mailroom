"""LegalBench evaluation suite — a second lens on model quality.

The suite runs LegalBench-style tasks over the locally-mirrored corpora
(full CUAD: 20,910 binary contract-QA pairs over 510 contracts + 200 labeled
contract texts), scores them deterministically, traces every run to Langfuse,
and appends each completed run to the experiment log + experiment-log site
(see legalbench/experiment_log.py).

It is a self-contained submodule: it reuses the vendored agent machinery
(retry contract, usage accounting) but adds no dependencies and never
obstructs the pipeline's own eval tasks.
"""

__version__ = "0.1.0"
