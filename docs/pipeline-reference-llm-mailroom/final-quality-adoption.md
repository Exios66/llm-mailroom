# Final pilot and field-support adoption (DMR-091)

The pilot subclass comparator, inventory and CUAD/MAUD normalization, pilot
classification counts, required-field presence/calibration, schema GT coverage
and field-audit means now delegate to additive Dojo APIs. Producer field
selection, catalog/prompt assembly, schema names, report usage/display means
and trace transport remain local. No prompts or catalog values change.

Required-field presence retains its literal legacy emptiness policy, which
counts expected []/{} as requirements, unlike GT schema coverage. Per-row calibration
retains absolute confidence error with three-decimal rounding and bool
confidence omitted. The pilot report sends supported raw pairs to Dojo
`pilot_calibration_error`, which rounds once after the mean; averaging rounded
row values would change the report. It is not replaced by binned ECE or a reporter-key rubric.
Audit emission consumes the real suite result's audit_support property and uses
one vote per eligible audited field, including actual zero rates.

The initial APIs shipped in Dojo `v0.23.0`; pipeline `v0.8.1` adopts the
corrected published `v0.23.1` through the normal pin workflow, from source
`04ac152a611606e687faa8b78528f3373f4c43a2`. Source suites, isolated subprocesses
and wheel-only scoring/import probes verify their named boundaries. They are
not a complete installed-producer or live-provider readiness claim. A real
wheel smoke test now verifies that its own taxonomy and all 18 frozen prompt
templates load without checkout fallbacks; source-only scripts and LegalBench
workflows remain outside the wheel. Final packaging evidence must distinguish
those limits.
