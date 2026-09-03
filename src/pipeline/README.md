# `pipeline/` — The plumbing that moves files and runs things

## What this folder is (plain English)

This is the **operational layer**: the long-running processes you start, and the filesystem "bins" that make the pipeline human-legible. If you `ls` into `data/pipeline/` you can literally see every document's current stage.

- `watcher.py` — the **main entrypoint**. Watches `data/pipeline/inbox/`; every new file gets its own LangGraph run in a background thread.
- `gmail_intake.py` — optional **Gmail intake channel**: polls the agent mailbox (IMAP SSL) and drops accepted attachments into the SAME inbox; runs inside `watcher.py` when `MAILROOM_GMAIL_ENABLED=1`. Full operator guide: [`docs/gmail-intake.md`](../../docs/gmail-intake.md).
- `bins.py` — all file-moving helpers. **This is the ONLY place files are moved.** Node code never calls `os.rename`/`shutil.move` directly.
- `config.py` — loads `config/taxonomy.yaml` (cached).
- `ops_monitor.py` — optional scheduled process that periodically checks for stuck documents / error spikes and asks the Boss agent for a health verdict.

## The bin layout (under `MAILROOM_BASE_DIR`, default `data/`)

```
pipeline/inbox/              ← new uploads land here
pipeline/processing/<id>/    ← claimed by a worker (atomic move)
pipeline/classified/<type>/  ← sorted, waiting for its specialist
pipeline/review/             ← needs a human decision
pipeline/failed/             ← unrecoverable errors
archive/<matter_id>/<type>/  ← final home for finished documents
manifests/<doc_id>.json      ← JSON sidecar per document
```

## Technical reference

- `watcher.py`
  - Uses `watchdog` to observe the inbox. Debounces duplicate events (1s), sleeps 0.5s before claiming, then `claim_file(path, worker_id)` → `run_pipeline(claimed, matter_id)` in a daemon thread.
  - `_infer_matter_id`: matter ID comes from the file's parent folder name, else a `FILENAME_MATTERID` pattern (`name_MATTERID.txt`), else `"DEFAULT"`.
  - Entry: `python pipeline/watcher.py` (blocking loop; Ctrl-C to stop).
- `bins.py`
  - Paths are built from `config/taxonomy.yaml` `pipeline.bins` templates with `{base_dir}` → `MAILROOM_BASE_DIR` env (default `./data`).
  - Key functions: `inbox_dir()`, `processing_dir(worker_id)`, `classified_dir(doc_type)`, `review_dir()`, `failed_dir()`, `archive_dir(matter_id, doc_type)`, `manifests_dir()`, `claim_file`, `move_to_classified`, `move_to_review`, `move_to_failed`, `move_to_archive`, `save_manifest`/`load_manifest`, `get_worker_id`, `list_inbox_files`.
  - Caches config at module level (`_config`) — restart the process after editing `taxonomy.yaml`.
- `config.py` — `load_config()` is `@lru_cache(maxsize=1)`; `get_agent_config`, `get_confidence_thresholds`, `get_all_doc_types`, `get_doc_class`, `get_extraction_schema_name`.
- `gmail_intake.py`
  - `poll_once()` sweeps `UNSEEN` messages over IMAP SSL (`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`), saves accepted extensions into the inbox with the `/upload` `<file>.meta` sidecar (`matter_id` from a subject `[M:<id>]` tag, else `MAILROOM_GMAIL_DEFAULT_MATTER_ID`), and marks the message `\Seen`. `Message-ID`s are remembered in `{base_dir}/gmail_intake_state.json` — a lost seen-mark can never double-queue.
  - Entry: embedded in the watcher via `start_embedded_poller()` (enabled-only); standalone `python pipeline/gmail_intake.py` (or `python -m pipeline.gmail_intake`) for debug. Status surfaces on `/health` as `checks.gmail_intake`.
- `ops_monitor.py`
  - `OpsMonitor._sweep()` gathers metrics (stuck docs from catalog, review/failed queue sizes) → `BossAgent.analyze_system_metrics()` → on `pause_ingestion` writes a pause-flag file `{base_dir}/ops_monitor_paused`.
  - **The watcher reads the flag**: `bins.py:is_ingestion_paused()` is checked by `watcher.py` on every new file (and during periodic inbox rescans) — paused ingestion leaves files in the inbox and resumes automatically once `/ops/resume` (API) or manual deletion clears the flag.
  - Interval from `OPS_MONITOR_INTERVAL_SECONDS` (default 300). Entry: `python pipeline/ops_monitor.py`.
