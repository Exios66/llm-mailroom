# Evidence for llm-mailroom#237 (Dojo 0.30.0 alignment, steps 0+1)

Synthetic inputs only. Run from an llm-mailroom checkout root (so `src` is importable) with
`python -I <script>`, once under a venv with Dojo `74b532a` and once with `93eedd8`.

- `mailroom_paths_impact.py`: merger rows through `build_expected_fields` -> `run_hf_pilot.labelled_fields`
  -> `score_row_extraction` -> `score_document_payload`. Outputs: `impact_base.json` (74b532a, 0.28.1),
  `impact_new.json` (93eedd8, 0.30.0). Source of the CHANGELOG / PR score table.
- `mailroom_paths_correspondence.py`: correspondence blank/"nan" label rows; `corr_*.json` identical.
- `merger_gold_diff.py`: mailroom `catalog_expected_fields` vs Dojo `hub_merger_expected_fields` on 8
  synthetic Hub rows (7 of 8 differ). Reusable for step 2 (D2) regression tests.
- `red.txt`, `red_startup.txt`: red runs of the new prompt-pin and startup tests against the old code.
