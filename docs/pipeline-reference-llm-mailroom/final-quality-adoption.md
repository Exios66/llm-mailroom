# Final pilot and field-support adoption (DMR-091)

The pilot subclass comparator, inventory and CUAD/MAUD normalization, pilot
classification counts, required-field presence/calibration, schema GT coverage
and field-audit means now delegate to additive Dojo APIs. Producer field
selection, catalog/prompt assembly, schema names, report usage/display means
and trace transport remain local. No prompts or catalog values change.

Required-field presence retains its literal legacy emptiness policy, which
counts expected []/{} as requirements, unlike GT schema coverage. Calibration
remains per-row absolute confidence error, with three-decimal rounding and bool
confidence omitted. It is not replaced by binned ECE or a reporter-key rubric.
Audit emission consumes the real suite result's audit_support property and uses
one vote per eligible audited field, including actual zero rates.

These APIs ship in published Dojo `v0.23.0`, adopted through the normal pin
workflow in pipeline `v0.8.1`. Full source suites and clean-install/subprocess
gates verify the consumer boundary; they do not certify live provider behavior.
