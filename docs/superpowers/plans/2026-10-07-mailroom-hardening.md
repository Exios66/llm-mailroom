# Mailroom Gmail Intake + LLM Layer Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Gmail intake path's silent-loss and infinite-retry failure modes, make outbound mail durable and well-formed, and make the free-model lane resilient, observable and fast, with the LLM-layer pieces (retry/fallback, served-model recording, quota breaker, result cache) applying to every node in the pipeline.

**Architecture:** Intake stays a polling thread in `pipeline/gmail_intake.py`; new focused modules own the new concerns (`mail_outbox.py` durable SQLite outbox, `mail_guards.py` auth/loop checks, `llm/quota.py` breaker, `llm/result_cache.py`). Outbound mail (echo, ack, reject, digest) all goes through the outbox. LLM resilience is added inside the existing `retry_chat_completion` ladder so every agent inherits it.

**Tech Stack:** Python 3.11, stdlib `imaplib`/`smtplib`/`sqlite3`, `openai` SDK (OpenRouter), pytest + `pytest-mock`, structlog.

**Spec:** the audit findings in this session (items 1–12), summarized per task below; no separate spec doc exists.

## Global Constraints

- Tests are hermetic: no sockets, no real SMTP/IMAP, no LLM. Use the `FakeIMAP` / `FakeSMTP` seams in `src/tests/test_gmail_intake.py` (extend them, don't replace) and the `temp_base_dir` fixture.
- Run tests with `cd /home/user/llm-mailroom && PYTHONPATH=src pytest src/tests/<file> -q`. Do not run the full suite per task. Per-task gate: `PYTHONPATH=src python src/scripts/affected_tests.py --files <changed files> --run -- -q` (tests importing the changed modules, depth 0). Branch gate after Task 7: `PYTHONPATH=src python src/scripts/affected_tests.py --base 91fe00a --depth -1 --run -- -q`.
- New env knobs use the `MAILROOM_GMAIL_*` prefix (or `MAILROOM_LLM_*` for the LLM layer), default to the safe behavior, and are added to `.env.example`.
- Never raise out of `poll_once`, `send_intake_echo`, or `dispatch_intake_echo` (existing contract).
- Paid models never rotate or fall back silently (existing contract in `llm/retry.py`); the quota breaker and `models` fallback apply to free models only.
- Add one `CHANGELOG.md` entry per task group, then run `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py` (the `--check` form is a test gate).
- Task order differs from the request's numbering on purpose: outbox and loop guards come before reject/ack replies because those replies must be durable and must never create backscatter.

## Review Focus

- Message with no `Message-ID` header: the fallback key is `uid:{folder}:{uidvalidity}:{uid}` (IMAP identity is UID within a mailbox and UIDVALIDITY epoch), so it never collides across folders or epochs. A changed UIDVALIDITY starts a new epoch and may re-ingest such mail once; that is accepted. Test (Task 1): two messages without IDs stay distinct after state reload, and the same UID under a new UIDVALIDITY gets a new key.
- Sender forms: `"Name" <A@Firm.Example>`, upper case, `+tag` addressing vs the allowlist. Test: allowlist match is case-insensitive on the bare address, with no plus-stripping (`a+tag@x` does not match an `a@x` entry, because the allowlist is a security control).
- Hostile filenames: `..`, backslashes, 300 chars, RFC 2047 non-ASCII, empty. Test: delivered name stays inside the inbox and is ≤ 120 chars.
- Hostile extracted text (`<script>`, `"`, `&`) rendered into echo, ack, digest HTML. Test: output contains no raw `<script>`.
- Process killed between enqueue and send, or between deliver and state save. Test: restart produces neither a duplicate email nor a duplicate inbox file.
- OpenRouter 429 with no `Retry-After` header. Test: breaker still opens using the default cooldown.

---

## Progress & execution rulings (as of 2026-10-07)

Executed subagent-driven (Sonnet implementers + Sonnet adversarial reviewers; Opus for fix rounds 4–5 and the final whole-branch review). Commits are staged by explicit path; tests are selected with `src/scripts/affected_tests.py` (never the full suite).

| Task | State | Commits |
|---|---|---|
| 1 Poison cap, same-fs staging | complete, CI green | 54f390a, 4460f8e, 68ce0da |
| 2 Durable outbox, echo dedup | complete, CI green | f1ef0c5, d867a51 |
| 3 DMARC + loop guards | next (first dispatch stopped before any change; re-dispatch) | — |
| 4–7 | pending | — |

Rulings that refine the task text below:
- **T1 transport errors:** only `imaplib.IMAP4.abort`, `socket.timeout`/`gaierror`/`herror`, `ConnectionError`, `ssl.SSLError`, `EOFError` abort a sweep without counting. Plain `IMAP4.error` (BAD/NO for one message) is per-message and counts toward the cap.
- **T1 promote without hard links:** `_place` falls back to `os.rename` after an `exists()` check + uniquify (`_rename_no_clobber`); the poller is the sole writer of its inbox names, so the check-then-rename window is accepted. Orphan `.part` files older than 1 h are swept at startup.
- **T2 delivery semantics:** at-least-once. Rows are claimed with a lease (`state='sending'`, `lease_until=now+300`) so two processes never send the same row; a crash mid-send delays retry up to 5 min. Dead rows are never auto-revived.
- **T2 echo build off the document path:** `dispatch_intake_echo` spawns one non-joined daemon thread that builds, enqueues and wakes the single `OutboxWorker`; failures log `gmail_echo_build_failed`. Sending stays on the worker.
- **CR1–CR10** (CodeRabbit review of this plan) are folded into the task text: exact-match allowlist (no plus-stripping), topmost-only `Authentication-Results` (T3), `uid:{folder}:{uidvalidity}:{uid}` key, dest-side `.part` on EXDEV, ordered-dedup `models` (T5), idempotent `record_group_result` and closed-group late echo (T7), two-phase delivery (T4).

Parked for the final fix pass:
- T1 finding 13 (sidecar written after placement) → handled by T4 two-phase ordering.
- T2 N1 `_claim` ignores `next_attempt_at`; N2 `user_version` written on every `_db()`; N3 concurrent first-open `ALTER` race; M2 restart test does not verify reload; M3 no mid-batch reconnect-failure test.

---

### Task 1: Poison-message cap and cross-device rename fix

**Files:**
- Modify: `src/pipeline/gmail_intake.py` (`_load_state`/`_save_state` ~388-410, `deliver_attachment` ~466-510, `poll_once` ~532-700, `_safe_filename` ~417), `.env.example`
- Test: `src/tests/test_gmail_intake.py`

**Interfaces:**
- Produces: `MAX_MESSAGE_ATTEMPTS: int` (env `MAILROOM_GMAIL_MAX_ATTEMPTS`, default 3); state JSON gains `"failed_attempts": {message_key: int}`; `_stage_path(ext: str) -> Path` (same-filesystem staging file under `inbox_dir().with_name(inbox_dir().name + ".staging")`, `.part` suffix so the watcher never claims it); `_place(src: Path, dest_dir: Path, name: str) -> Path` (atomic no-clobber placement, uniquifies, falls back on `OSError` with `errno.EXDEV` to copying into a destination-side `.part` file, then promoting it with the same no-clobber link, or with `os.rename` after an `exists()` check + uniquify when the filesystem has no hard links; the `.part` is removed on any failure, so a partial file never sits under a watcher-visible name); the missing-Message-ID key becomes `uid:{folder}:{uidvalidity}:{uid}`; `_quarantine(client, uid: str, message_key: str, reason: str) -> None` (adds to processed, marks `\Seen`, applies Gmail label `mailroom/failed` via `X-GM-LABELS`); `report["quarantined"]: int`. `_safe_filename` returns names ≤ 120 chars with `\` and `..` segments neutralized.

- [x] **Step 1: Write failing tests**
  - `test_exdev_rename_falls_back_to_copy`: monkeypatch both `os.link` and `os.replace` to raise `OSError(errno.EXDEV, "x")` once, so the copy branch runs whichever primitive `_place` uses; poll a one-PDF message; assert file in inbox, no `.part` left in the staging or inbox dir, `report["attachments_queued"] == 1`.
  - `test_exdev_copy_failure_leaves_no_partial_in_inbox`: copy raises mid-way → inbox holds neither the file nor a `.part`.
  - `test_missing_message_id_key_scoped_by_folder_and_uidvalidity`.
  - `test_message_that_raises_is_quarantined_after_cap`: monkeypatch `deliver_attachment` to raise `RuntimeError`; poll 3 times with `MAILROOM_GMAIL_MAX_ATTEMPTS=3`; assert polls 1–2 leave `uid` unseen and `report["errors"]` non-empty, poll 3 gives `report["quarantined"] == 1`, the uid is in `FakeIMAP.seen`, `FakeIMAP.labels[uid]` contains `mailroom/failed`, and a 4th poll sees 0 messages.
  - `test_temp_file_removed_when_deliver_raises`: same patch; assert staging dir is empty after the poll.
  - `test_state_saved_per_message_survives_mid_sweep_crash`: two messages, second one raises `KeyboardInterrupt`-free fatal via patching `_fetch_message`; assert first message's ID is in the saved state file.
  - `test_safe_filename_neutralizes_traversal`: `_safe_filename("..\\..\\x"*40)` has no `\`, no `..`, `len ≤ 120`.
  - `FakeIMAP`: extend `STORE` to record labels for `mailroom/failed` (already handles `X-GM-LABELS`).

- [x] **Step 2: Run to verify failure** — `PYTHONPATH=src pytest src/tests/test_gmail_intake.py -q -k "exdev or quarantined or temp_file or per_message or traversal"`; expected FAIL (missing names / behavior).

- [x] **Step 3: Implement** the Interfaces above. Approach: stage with `tempfile.NamedTemporaryFile(dir=staging, suffix=".part", delete=False)` (replaces `mktemp`); `_place` tries `os.link`+`unlink` for no-clobber atomicity, on `FileExistsError` increments the counter, on `EXDEV` copies into a dest-side `.part` and promotes it with the same no-clobber link; wrap each message's work in `try/finally` that unlinks its staged files; increment `failed_attempts[message_key]` in the per-message `except`, call `_quarantine` at cap; call `_save_state` after every message instead of once at the end.

- [x] **Step 4: Run** the new tests plus the whole file: `PYTHONPATH=src pytest src/tests/test_gmail_intake.py -q`; expected all PASS.

- [x] **Step 5: Commit** — `git add <changed paths> && git commit -m "fix(gmail): poison-message cap, same-fs staging, per-message state save"`

---

### Task 2: Durable outbox and echo dedup

**Files:**
- Create: `src/pipeline/mail_outbox.py`
- Modify: `src/pipeline/gmail_intake.py` (`send_intake_echo` ~1340-1428, `dispatch_intake_echo` ~1431, remove `_ECHO_DONE`/`_ECHO_LOCK` ~799-800), `src/pipeline/watcher.py` (start/stop the worker next to `start_embedded_poller`)
- Test: `src/tests/test_mail_outbox.py` (new), update `src/tests/test_gmail_intake.py` echo tests

**Interfaces:**
- Produces (`mail_outbox.py`):
  - `@dataclass OutboxRow: dedup_key, to_addr, subject, headers: dict[str,str], text, html, attempts: int`
  - `enqueue(dedup_key: str, *, to_addr: str, subject: str, text: str, html: str | None, headers: dict[str, str] | None = None) -> bool` — `INSERT OR IGNORE`; False when the key already exists (any state).
  - `drain_once(smtp_factory=None, now: float | None = None, limit: int = 10) -> dict` returning `{"sent": n, "failed": n, "dead": n}`; one SMTP connection reused per call; success → state `sent`; failure → `attempts += 1`, `next_attempt_at = now + min(3600, 30 * 2**attempts)`, state `dead` after `MAX_ATTEMPTS = 8`.
  - `count_recent(to_addr: str, window_s: float, now: float | None = None) -> int` (rows created for that recipient in the window; used by Task 3).
  - `class OutboxWorker(threading.Thread)` with `wake()` and `stop()`; `start_outbox_worker() -> OutboxWorker | None` (no-op when the echo channel is disabled).
  - SQLite file `get_base_dir()/mail_outbox.sqlite`, WAL mode, table `outbox(dedup_key PRIMARY KEY, to_addr, subject, headers_json, text, html, state, attempts, next_attempt_at, last_error, created_at, sent_at)`.
- Consumes: `gmail_intake._INJECTED_SMTP_FACTORY`, `load_config()["smtp_host"/"smtp_port"/"address"/"password"]`.
- Changes: `send_intake_echo(manifest) -> bool` now builds the message, `enqueue("echo:{doc_id}:{stage}", ...)`, then `drain_once()` inline and returns True iff the row is `sent`; `dispatch_intake_echo` enqueues and calls `worker.wake()` (no thread-per-echo).

- [x] **Step 1: Write failing tests** (`test_mail_outbox.py`, using `temp_base_dir`)
  - `test_enqueue_is_idempotent_across_restart`: enqueue key `k`, call `importlib.reload`-free reopen by calling `enqueue("k", ...)` again → returns False; `drain_once(FakeSMTP)` sends exactly 1.
  - `test_failed_send_retried_after_backoff`: SMTP stub raises on first connect; `drain_once(now=0)` → `failed == 1`; `drain_once(now=10)` → `sent == 0` (backoff not elapsed); `drain_once(now=61)` → `sent == 1`.
  - `test_row_goes_dead_after_max_attempts`: always-failing stub, advance `now` past each backoff 8 times → last call reports `dead == 1`, later drains send nothing.
  - `test_one_smtp_connection_per_drain`: 3 enqueued rows, factory call count == 1.
  - Update the existing `test_send_intake_echo_replies_on_source_thread` unchanged in assertions (still synchronous send via inline drain); add `test_echo_not_resent_after_restart` (call twice with a fresh process state → `FakeSMTP.sent` length 1).

- [x] **Step 2: Run to verify failure** — `PYTHONPATH=src pytest src/tests/test_mail_outbox.py -q`; expected FAIL (module missing).
- [x] **Step 3: Implement** the module and the `gmail_intake` changes. Approach: `sqlite3` with `check_same_thread=False` and a module lock; one connection helper; `Message-ID` header of outgoing mail changes from `@mailroom.local` to `@{domain of cfg["address"]}`.
- [x] **Step 4: Run** new tests + `PYTHONPATH=src pytest src/tests/test_gmail_intake.py src/tests/test_status_notify.py -q`; expected PASS.
- [x] **Step 5: Commit** — `git add <changed paths> && git commit -m "feat(gmail): durable SQLite outbox with backoff and idempotent echo dedup"`

---

### Task 3: DMARC check and loop guards

**Files:**
- Create: `src/pipeline/mail_guards.py`
- Modify: `src/pipeline/gmail_intake.py` (`poll_once` sender block ~597-605, `_sender_address` ~445), `.env.example`
- Test: `src/tests/test_mail_guards.py` (new); extend `test_gmail_intake.py` helper `_message(..., extra_headers=None)`

**Interfaces:**
- Produces:
  - `@dataclass AuthVerdict: dmarc: str | None, dkim: str | None, spf: str | None`
  - `parse_authentication_results(values: list[str], authserv_id: str = "mx.google.com") -> AuthVerdict` — `values` in message order (topmost first). Gmail prepends its own header above anything the sender supplied, so ONLY the topmost header is trusted: if `values[0]`'s first token is not `authserv_id`, return an all-`None` verdict (fail closed). Lower headers, even ones claiming `mx.google.com`, are never read.
  - `automated_reason(msg: email.message.Message, own_address: str) -> str | None` — non-None for: `Auto-Submitted` ≠ `no`; `Precedence` in `bulk|list|junk`; `List-Id` or `List-Unsubscribe` present; `X-Auto-Response-Suppress` present; `Return-Path: <>`; `multipart/report` (DSN); From local-part in `mailer-daemon|postmaster|no-reply|noreply`; sender equals `own_address`.
  - `sender_permitted(allowed: set[str], sender: str, verdict: AuthVerdict, require_dmarc: bool) -> tuple[bool, str]` — empty allowlist → `(True, "open")`; otherwise requires case-insensitive bare-address match and, when `require_dmarc`, `verdict.dmarc == "pass"`.
  - `reply_allowed(to_addr: str, max_per_hour: int) -> bool` using `mail_outbox.count_recent`.
  - Config: `MAILROOM_GMAIL_REQUIRE_DMARC` (default `1`), `MAILROOM_GMAIL_MAX_REPLIES_PER_HOUR` (default `20`); `report["skipped_automated"]`, `report["skipped_auth"]`.
- [ ] **Step 1: Write failing tests**
  - `test_forged_auth_results_header_ignored`: values `["mx.google.com; dmarc=fail", "mx.google.com; dmarc=pass"]` (forged pass below Gmail's) → `dmarc == "fail"`.
  - `test_untrusted_topmost_auth_results_fails_closed`: values `["evil.example; dmarc=pass", "mx.google.com; dmarc=pass"]` → `dmarc is None`, and `sender_permitted(..., require_dmarc=True)` → `(False, "dmarc")`.
  - `test_allowlist_requires_dmarc_pass`: allowlisted sender with `dmarc=fail` → `(False, "dmarc")`; with `pass` → True; case/`"Name" <A@Firm.Example>` forms match.
  - `test_automated_reason_cases`: one assertion per header listed above, plus a normal human message → `None`.
  - `test_poll_skips_automated_without_reply`: message with `Auto-Submitted: auto-replied` → marked seen, `skipped_automated == 1`, no inbox file, outbox empty.
  - `test_plus_tag_does_not_match_bare_allowlist_entry`.
  - `test_reply_budget`: enqueue 20 rows for one recipient → `reply_allowed(...) is False`.
- [ ] **Step 2: Run to verify failure** — `PYTHONPATH=src pytest src/tests/test_mail_guards.py -q`; expected FAIL.
- [ ] **Step 3: Implement** the module and wire `poll_once` (automated check first, then sender/DMARC, both mark seen + record processed). Extend the `FakeIMAP` message helper to emit an `Authentication-Results: mx.google.com; dmarc=pass` header by default so existing tests keep passing.
- [ ] **Step 4: Run** `PYTHONPATH=src pytest src/tests/test_mail_guards.py src/tests/test_gmail_intake.py -q`; expected PASS.
- [ ] **Step 5: Commit** — `git add <changed paths> && git commit -m "feat(gmail): DMARC-gated allowlist and automated-mail loop guards"`

---

### Task 4: Reject replies and retryable I/O errors

**Files:**
- Modify: `src/pipeline/gmail_intake.py` (`deliver_attachment`, `poll_once`, new `build_reject_reply`, `enqueue_reject_reply`)
- Test: `src/tests/test_gmail_intake.py`

**Interfaces:**
- Consumes: `mail_outbox.enqueue`, `mail_guards.reply_allowed`, `mail_guards.automated_reason`.
- Produces:
  - `deliver_attachment(...) -> tuple[str | None, str | None]` now also returns reason `"io"` when an `OSError` occurs (never raises for I/O).
  - `build_reject_reply(subject: str, rejected: list[tuple[str, str]], accepted_exts: list[str], max_mb: int) -> tuple[str, str]` → `(text, html)`; reasons map to plain sentences (`extension`, `size`, `filename`, `none`), all values HTML-escaped.
  - `enqueue_reject_reply(cfg: dict, *, message_id: str, sender: str, subject: str, rejected: list[tuple[str, str]]) -> bool` with dedup key `reject:{message_id}`; only for senders that passed the allowlist/DMARC check, never for automated mail, and subject to `reply_allowed`.
  - Poll rule (two-phase, no rollback of published files): stage EVERY accepted attachment of a message (Task 1 `_stage_path`) before publishing any. If any staging step returns `"io"`, unlink only that message's staged `.part` files (never visible to the watcher) and raise `GmailIntakeError("io")` so Task 1's attempt counter handles it; the message stays unseen and is not added to processed. Only after all are staged, promote each with `_place` (file then sidecar). Files already in the inbox are never unlinked, because the watcher may have claimed them. If a promote fails part-way, the message is recorded processed with `report["errors"]` naming the unpromoted files, and is not retried, so no attachment is ever queued twice.
- [ ] **Step 1: Write failing tests**
  - `test_unaccepted_extension_gets_reject_reply`: `.exe` only → one outbox row to the sender with subject `Re: <subject>` whose text lists `.exe` and the accepted extensions; message marked seen.
  - `test_no_attachments_gets_reject_reply` (`none` reason).
  - `test_non_allowlisted_sender_gets_no_reply` (backscatter guard).
  - `test_io_error_during_staging_publishes_nothing_and_retries`: two PDFs, second staging write hits `OSError` → inbox empty, staging empty, uid unseen, attempts == 1; next poll with the error removed delivers both exactly once.
  - `test_promote_failure_never_unlinks_published_file`: second `_place` raises → first file still in inbox, message processed, `report["errors"]` names the second file, next poll delivers nothing new.
  - `test_reject_reply_html_escapes_filename`: filename `<script>x</script>.exe` → html has no raw `<script>`.
- [ ] **Step 2: Run to verify failure**, **Step 3: Implement**, **Step 4: Run** `PYTHONPATH=src pytest src/tests/test_gmail_intake.py -q` (PASS).
- [ ] **Step 5: Commit** — `git add <changed paths> && git commit -m "feat(gmail): reject replies for sender-fixable errors; I/O errors retry instead of dropping"`

---

### Task 5: OpenRouter `models` fallback, served-model recording, free-quota breaker

**Files:**
- Create: `src/llm/quota.py`
- Modify: `src/llm/retry.py` (`retry_chat_completion` ~160-245), `src/pipeline/limits.py` (`record_usage`, `usage_summary` ~61-110), `src/agents/base.py` (~177-181), `src/agents/gmail_triage.py` (fail-soft path in `GmailTriageAgent`; read the class first), `src/config/taxonomy.yaml` (`free_quota:` block, `free_model_swarm:` ordering), `.env.example`
- Test: `src/tests/test_llm_retry.py`, `src/tests/test_llm_free_swarm.py`, `src/tests/test_run_limits.py`, `src/tests/test_llm_quota.py` (new)

**Interfaces:**
- Produces:
  - `quota.py`: `class FreeQuotaExhausted(RuntimeError)`; `class FreeQuotaBreaker` with `note_rate_limit(retry_after_s: float | None = None, now: float | None = None) -> None`, `note_success() -> None`, `is_open(now: float | None = None) -> bool`, `open_until: float`; `get_breaker() -> FreeQuotaBreaker` (module singleton, lock-guarded); trips after `free_quota.trip_after` (default 3) consecutive 429s; cooldown = `retry_after_s` if given else `free_quota.cooldown_s` (default 300), doubling per consecutive trip, capped at 3600.
  - `retry_chat_completion`: for free models on an OpenRouter base URL, when the swarm has more than the primary, merge `extra_body={"models": ordered_dedup([primary, *swarm]), "provider": {"require_parameters": True}}` into the call (preserving an existing `extra_body`), so the server falls back; client-side rotation stays for non-OpenRouter URLs. Before each free call, raise `FreeQuotaExhausted` if `get_breaker().is_open()`; call `note_rate_limit` on 429s and `note_success` on success. Paid models never touch the breaker.
  - `record_usage(usage, model=None, agent=None, served_model: str | None = None)`; `usage_summary()["by_agent"][agent]["served_models"]: list[str]`; `BaseAgent._call_llm` passes `getattr(response, "model", None)` and logs it as `served_model` in `llm_response`.
  - Triage: on `FreeQuotaExhausted` the agent returns the deterministic header-extraction result with `degraded: "free_quota"` in the triage payload (no paid fallback, no raise).
- [ ] **Step 1: Write failing tests**
  - `test_breaker_opens_after_three_429s_and_closes_after_cooldown` (injected `now`); `test_breaker_uses_default_cooldown_without_retry_after`; `test_success_resets_consecutive_count`.
  - `test_openrouter_free_call_sends_models_array`: fake client records kwargs; swarm `["a:free","b:free"]`, model `a:free`, base_url `https://openrouter.ai/api/v1` → `kwargs["extra_body"]["models"] == ["a:free","b:free"]` and `provider.require_parameters is True`; existing `extra_body` keys preserved.
  - `test_paid_model_gets_no_models_array_and_ignores_breaker`.
  - `test_open_breaker_short_circuits_free_call`: client create never called, `FreeQuotaExhausted` raised.
  - `test_served_model_recorded_in_usage_summary`: response with `.model == "x/y:free"` while requested `openrouter/free` → `served_models == ["x/y:free"]`.
  - `test_triage_degrades_to_deterministic_when_quota_open` (extend `test_gmail_triage.py` fixtures).
- [ ] **Step 2: Run to verify failure** — `PYTHONPATH=src pytest src/tests/test_llm_quota.py src/tests/test_llm_retry.py src/tests/test_llm_free_swarm.py src/tests/test_run_limits.py -q`.
- [ ] **Step 3: Implement.** Keep `openrouter/free` as the last swarm entry; the ranked free-model list ahead of it is a taxonomy edit made after scoring candidates in `llm-dojo-scoring` (decision for the owner; ship with `openrouter/free` alone, which keeps behavior unchanged).
- [ ] **Step 4: Run** the four files plus `src/tests/test_gmail_triage.py`; expected PASS.
- [ ] **Step 5: Commit** — `git add <changed paths> && git commit -m "feat(llm): models fallback, served-model recording, free-quota breaker"`

---

### Task 6: BODYSTRUCTURE pre-filter, IMAP IDLE, LLM result cache

**Files:**
- Create: `src/llm/result_cache.py`
- Modify: `src/pipeline/gmail_intake.py` (`_fetch_message`, `poll_once`, `GmailIntakePoller.run`), `src/agents/gmail_triage.py` (before `_call_structured`), `.env.example`
- Test: `src/tests/test_gmail_intake.py`, `src/tests/test_result_cache.py` (new); extend `FakeIMAP` with `BODYSTRUCTURE`, `BODY.PEEK[]`, and an `idle()` seam

**Interfaces:**
- Produces:
  - `@dataclass PartInfo: name: str | None, size: int, maintype: str`
  - `parse_bodystructure(raw: bytes) -> list[PartInfo]` (pure; returns `[]` when unparseable, which triggers the full-fetch fallback)
  - `_fetch_structure(client, uid: str) -> list[PartInfo]`; `_fetch_message` switches to `BODY.PEEK[]` (does not set `\Seen`)
  - Pre-filter rule: classify each named part by extension and size from structure; if no part is acceptable, skip the body download and go straight to the reject path (Task 4).
  - `idle_wait(client, timeout_s: float) -> bool` (True = server pushed new mail; raw IDLE over `client.send`/`readline`, re-issued every ≤ 5 min); poller uses it only when `MAILROOM_GMAIL_IDLE=1` (default `0` until verified with `gmail_smoke_test.py --real`) and falls back to `_stop_event.wait(poll_seconds)` on any error.
  - `result_cache.py`: `cache_key(model_tag: str, prompt: str, doc_text: str) -> str` (sha256 hex); `get(key: str) -> dict | None`; `put(key: str, value: dict) -> None`; SQLite at `get_base_dir()/llm_result_cache.sqlite`, TTL 30 days, `MAILROOM_LLM_CACHE=0` disables. Triage consults it before the LLM call and stores validated results only.
- [ ] **Step 1: Write failing tests**
  - `test_parse_bodystructure_real_gmail_sample`: use a recorded two-attachment Gmail `BODYSTRUCTURE` literal committed under `src/tests/fixtures/`; assert names and sizes.
  - `test_oversize_only_message_never_downloads_body`: `FakeIMAP` counts `BODY.PEEK[]` fetches → 0, reject reply enqueued.
  - `test_fetch_uses_peek_and_does_not_mark_seen_until_handled`.
  - `test_unparseable_structure_falls_back_to_full_fetch`.
  - `test_poller_uses_idle_when_enabled_and_falls_back_on_error` (fake `idle_wait` raising).
  - `test_result_cache_hit_skips_llm`: second triage of identical text makes 0 client calls; `test_cache_key_changes_with_prompt`; `test_cache_ttl_expiry` (injected clock); `test_cache_disabled_by_env`.
- [ ] **Step 2: Run to verify failure**, **Step 3: Implement**, **Step 4: Run** `PYTHONPATH=src pytest src/tests/test_gmail_intake.py src/tests/test_result_cache.py src/tests/test_gmail_triage.py -q` (PASS).
- [ ] **Step 5: Commit** — `git add <changed paths> && git commit -m "perf(gmail): BODYSTRUCTURE pre-filter, opt-in IDLE, triage result cache"`

---

### Task 7: Acknowledgment email and bundle digest

**Files:**
- Modify: `src/pipeline/gmail_intake.py` (`poll_once` after routing ~625-676, `send_intake_echo`, new `build_ack_email`, `build_digest_html`/`build_digest_text`), `src/pipeline/mail_outbox.py` (group tables), `src/pipeline/watcher.py` (`_INTAKE_META_KEYS`: add `group_size`)
- Test: `src/tests/test_gmail_intake.py`, `src/tests/test_mail_outbox.py`

**Interfaces:**
- Consumes: `mail_outbox.enqueue`, `_processing_timeline`, `build_echo_html` helpers (reuse their escaping), `reply_allowed`.
- Produces:
  - `build_ack_email(subject: str, items: list[dict], route: str) -> tuple[str, str]` (items carry `filename`, `upload_id`); sent once per message via `enqueue("ack:{message_id}", ...)` when ≥ 1 attachment is queued. Copy states the expected path (free triage vs full pipeline) and the doc IDs.
  - Sidecar/intake meta gains `group_size: int` (number of queued attachments for the message).
  - `mail_outbox`: tables `message_groups(message_id PRIMARY KEY, expected, sender, subject, created_at)` and `group_results(message_id, doc_id, stage, summary_json, PRIMARY KEY(message_id, doc_id))`; `record_group_result(message_id: str, doc_id: str, stage: str, summary: dict) -> tuple[int, int]` returning `(have, expected)` (a duplicate `(message_id, doc_id)` is a no-op that returns the current counts); `flush_stale_groups(now: float, max_age_s: float = 21600) -> list[str]` marks each flushed group closed (`message_groups.closed_at`).
  - `send_intake_echo`: when `intake["group_size"] > 1`, record the result and send nothing until `have == expected`, then enqueue one `digest:{message_id}` (table of per-document class, confidence, outcome, and a footer naming the model that answered); the worker's sweep calls `flush_stale_groups` and sends a partial digest marked "incomplete". A result that arrives for a closed group gets the normal per-document echo (`echo:{doc_id}:{stage}`), never a second digest. Single-document messages behave exactly as before.
- [ ] **Step 1: Write failing tests**
  - `test_single_doc_message_gets_ack_then_echo` (two outbox rows, keys `ack:` and `echo:`).
  - `test_three_doc_bundle_gets_one_ack_and_one_digest`: terminal manifests for 3 doc IDs → after the 3rd, exactly one `digest:` row; none before.
  - `test_stale_group_flushes_partial_digest` (injected `now`, 2 of 3 done).
  - `test_late_result_after_partial_digest_gets_own_echo`.
  - `test_duplicate_terminal_manifest_is_idempotent`: repeat a manifest → no error, unchanged counts, one `digest:` row.
  - `test_digest_escapes_hostile_extracted_values` (`<script>`, `&`, `"` in `extracted_data`).
  - `test_digest_and_ack_are_multipart_with_text_part` (parse the queued message, assert both parts).
  - `test_group_size_survives_watcher_intake_meta_whitelist`.
- [ ] **Step 2: Run to verify failure**, **Step 3: Implement**, **Step 4: Run** the affected-test gate on the changed files (PASS), then `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py --check`.
- [ ] **Step 5: Commit** — `git add <changed paths> && git commit -m "feat(gmail): acknowledgment email and one-digest-per-bundle replies"`

---

### Final verification (after Task 7)

- [ ] `PYTHONPATH=src python src/scripts/affected_tests.py --base 91fe00a --depth -1 --run -- -q` is green; `PYTHONPATH=src python src/scripts/gmail_smoke_test.py` passes (offline mode).
- [ ] With real credentials only when the owner approves: `PYTHONPATH=src python src/scripts/gmail_smoke_test.py --real`, then toggle `MAILROOM_GMAIL_IDLE=1` and watch pickup latency.

## Out of scope (follow-up plan after profiling)

Node-level hardening outside the Gmail/LLM layers (watcher stale-claim reclaim, `build_graph` per-node timeouts, catalog contention, `status_notify.py` outbox migration and its hard-coded default recipient) needs measurements first; propose a second plan once the soak test (100–500 mixed messages: p50/p95 time to first and final email, memory, free-quota burn) exists.

## Self-review notes

- Coverage: all seven requested items map to Tasks 1–7 (requested #3→T2, #7→T3, #2→T4, #4→T5, #5→T6, #6→T7, #1→T1).
- Known unverified assumptions to confirm during execution: Gmail's literal `BODYSTRUCTURE` shape (hence the recorded fixture and full-fetch fallback), raw IDLE over `imaplib` (hence opt-in), and the triage agent's fail-soft code location (Task 5 says to read it first).
