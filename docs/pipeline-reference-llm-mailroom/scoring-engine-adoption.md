# Core scoring engine adoption (DMR-091)

The pipeline adopts published Dojo `v0.23.1` from source main
`04ac152a611606e687faa8b78528f3373f4c43a2`. Its `pipeline_scoring`,
`legalbench_scoring` and `agent_rubrics` APIs own primary quality computation.
The dependency pin was updated with `bump_dojo_scoring.py --apply --tag v0.23.1`
at the scoring release boundary. Pipeline `v0.8.1` records this compatible
consumer release. No prompt, provider, SDK or model is promoted.

`observability.classification_scoring` and `legalbench.scoring` are compatible
Dojo facades. `scripts.run_experiment.compute_metrics` delegates the complete
classification/stage report, preserving four-decimal union-class macro F1 and
selected stage support. Exact class sequence mismatch now raises rather than
silently truncating GT; aligned keys remain exact aliases at three decimals.

`legalbench.runner._tokens_summary` projects the historical usage fields into
Dojo's existing `cost.tokens_summary`, then selects its six old keys. Empty
usage dictionaries are ignored, known zero cost participates, total tokens are
not reconstructed from prompt/completion tokens, and mean cost differs from
total cost. This report never consumed cost-basis labels or price estimates,
so the projection intentionally omits those additive fields. Nonfinite cost
propagates as before; nonfinite token conversion raises as before.

`observability.agent_eval` retains invocation/serialization/report rounding
and delegates token SET F1, serialized key substring presence and enum validity.
`observability.honest_gaps` delegates the quality-population homogeneity gate
used by `observability.local_eval_packs` and HF reports. Fixture hydration and
presentation means in local packs remain local. Producer invariant issues
(unknown determination, denied without reasons, etc.) remain local; they do
not implement the registered structural scoring metric.

LegalBench trusts producer `correct` flags for supported rows, including
inconsistent flags. Multiclass support requires an ok row with nonblank expected
and predicted labels and a prediction other than `error`; missing/blank labels
are excluded and counted in `n_error`, including confidence support. The
pipeline supplies its established subtype-equivalence groups explicitly without
mutating process-wide Dojo settings. These APIs were introduced in v0.23.0;
v0.23.1 adds the corrected label boundary and explicit producer-group API.
Nonfinite/out-of-range confidences are excluded from report ECE/mean. Direct
`_ece` invalid-input behavior is retained. The visualizer has a separate literal
class/subclass/lifecycle contract; these APIs do not silently unify it.

The source regression `src/tests/test_core_engine_delegation.py` hand-counts
reports and records calls into real Dojo functions. Run the existing LegalBench,
classification, local-pack and experiment suites alongside it. Full source
and dependent gates remain mandatory after the dependency is adopted. Production
corporate v6 and eval corporate v7 bytes and identities remain unchanged.
