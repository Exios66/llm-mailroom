# Gmail Intake Route — the agent mailbox as a second intake (HUB-037)

The mailroom's agent mailbox — `llmmailroom@gmail.com` — is a full second
intake route alongside the API `POST /v1/upload` and the filesystem inbox.
Email a document to the mailbox and the mailroom sweeps it in, processes it,
archives it in the auditable hash archive, and reports the outcome **on the
same email thread**.

Single-document uploads are handled by the **free OpenRouter triage team**
(`z-ai/glm-5.2:free` — $0): the core pipeline steps run without any paid
agent. This guide is the complete operator/sender manual: how to enable the
channel, how to format an upload email (subject-line contract included),
every pathway a document can take from Gmail into the pipeline, and how to
operate and troubleshoot the channel.

Code map:

| Piece | File |
|:---|:---|
| IMAP poller / echo / reactions | `src/pipeline/gmail_intake.py` |
| Watcher claim + triage dispatch + capability pre-check | `src/pipeline/watcher.py` |
| Free triage agent | `src/agents/gmail_triage.py` |
| Connectivity smoke test | `src/scripts/gmail_smoke_test.py` |
| Environment variables | `docs/configuration.md` |

---

## 1. Enable the channel

The channel is **explicit opt-in** — it never starts polling on its own.

1. **Gmail account with 2FA + app password.** The mailbox is a normal Gmail
   account. Enable 2-Step Verification, then create an **App Password**
   (Google Account → Security → 2-Step Verification → App passwords). The
   16-character password is what IMAP/SMTP authenticate with; the displayed
   grouping spaces are tolerated and stripped automatically.
2. **Configure `.env`** (gitignored — credentials NEVER go in argv, code, or
   git):

   ```bash
   MAILROOM_GMAIL_ENABLED=1
   GMAIL_ADDRESS=llmmailroom@gmail.com
   GMAIL_APP_PASSWORD=abcdefghijklmnop        # 16 chars; spaces tolerated/stripped
   # Optional (defaults shown):
   # MAILROOM_GMAIL_POLL_SECONDS=60           # sweep interval
   # MAILROOM_GMAIL_MAX_ATTACHMENT_MB=50      # per-attachment cap
   # MAILROOM_GMAIL_DEFAULT_MATTER_ID=DEFAULT # matter when the subject has no [M:] tag
   # MAILROOM_GMAIL_ALLOWED_SENDERS=          # CSV allowlist; empty = accept all
   # MAILROOM_GMAIL_REACTIONS=1               # ✅ claim acknowledgement
   # MAILROOM_GMAIL_REACTION_LABEL=✅
   # MAILROOM_GMAIL_ECHOES=1                  # completion-report replies
   # MAILROOM_GMAIL_TRIAGE=1                  # free single-document triage lane
   # MAILROOM_GMAIL_SMTP_HOST=smtp.gmail.com  # echo SMTP
   # MAILROOM_GMAIL_SMTP_PORT=465
   ```

3. **Run the watcher.** The poller runs INSIDE the watcher process — the
   `watcher.lock` holder stays the single intake authority:

   ```bash
   PYTHONPATH=src python -m api.main          # API embeds the watcher (default)
   # or a dedicated watcher:
   PYTHONPATH=src python -m pipeline.watcher
   # standalone poller (debug/ops only — never alongside a lock-holding watcher):
   PYTHONPATH=src python -m pipeline.gmail_intake
   ```

4. **Verify.** `GET /health` reports the channel under `checks.gmail_intake`
   (`enabled` / `running` / `last_poll_at` / counters — never credentials).
   End-to-end proof without spending tokens:

   ```bash
   PYTHONPATH=src python src/scripts/gmail_smoke_test.py          # mock: fake IMAP, scratch bins
   PYTHONPATH=src python src/scripts/gmail_smoke_test.py --real   # sends a real FNOL email (quiet mailbox!)
   ```

**CI secrets.** `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` are registered via
`gh secret set` on `Exios66/mailroom-dev` **and** `Exios66/llm-mailroom` for
workflow use — never committed.

**Security best practices**

- The app password grants full mailbox access — store it ONLY in `.env` (or
  the GitHub secret manager); rotate it like any credential.
- Set `MAILROOM_GMAIL_ALLOWED_SENDERS` in production so only known senders
  can queue documents; the allowlist compares lowercased addresses.
- Reactions/echoes reveal document status to the original sender only
  (echoes reply To: the sender, threaded via `In-Reply-To`); keep the
  mailbox address unlisted.

---

## 2. Uploading a document — instructions & best practices

There is **no subject keyword to trigger pickup**. Every email arriving at
the mailbox is swept automatically (every `MAILROOM_GMAIL_POLL_SECONDS`,
default 60 s); only **unseen** messages are processed. `RE:` / `FWD:`
prefixes are irrelevant. The rules that DO matter:

| Rule | Detail |
|:---|:---|
| **Attach the document** | Only attachments are processed — the email body is never read. Body-only emails are marked seen and skipped (logged as `gmail_message_no_processable_attachments`) |
| **Accepted extensions** | `file_extensions` from `config/taxonomy.yaml`: `.pdf`, `.txt`, `.docx`, `.md`, `.jpg`, `.jpeg`, `.png`, `.gif`. Anything else is skipped (message still acknowledged) |
| **Size** | ≤ `MAILROOM_GMAIL_MAX_ATTACHMENT_MB` (default **50 MB**) per attachment; oversized attachments are skipped, message still acknowledged |
| **Sender** | Any mailbox can send unless `MAILROOM_GMAIL_ALLOWED_SENDERS` is set (CSV, lowercased comparison) |
| **Subject matter tag** | Optional `[M:<matter_id>]` — see § Subject line below |
| **One email = one document** | Best practice for traceability: send each document as its own email with one attachment |

### Subject-line formatting

The only meaningful formatting is the optional **matter-routing tag**:

```
[ M : <matter_id> ]
```

- **Placement:** anywhere in the subject — beginning, middle, or end.
- **Grammar:** the first `[M:<matter_id>]` token wins; `<matter_id>` allows
  **1–64 characters** of `A-Z a-z 0-9 _ . -` (no spaces, slashes, or other
  punctuation inside the tag).
- **Effect:** the document files under that matter
  (`archive/<matter_id>/<doc_type>/`, `session_id = matter_id` in tracing).
- **Without the tag** the document files under
  `MAILROOM_GMAIL_DEFAULT_MATTER_ID` (default `DEFAULT`).

Examples:

```
Subject: Hail damage FNOL [M:MORNINGSTAR-001]        → matter MORNINGSTAR-001
Subject: [M:ACME-2026.04] Signed consulting agreement → matter ACME-2026.04
Subject: Invoice scan                                 → matter DEFAULT
```

Bad tags (fall back to `DEFAULT` — the literal text is not a valid matter):
`[M:Smith 001]` (space), `[M:Smith/001]` (slash), `[M:]` (empty).

### Worked example

```
To:      llmmailroom@gmail.com
Subject: Hail damage FNOL [M:MORNINGSTAR-001]
Attach:  claim_2026-03-14.pdf
```

**Sender best practices**

- One document per email — each accepted attachment becomes its own
  document under the same matter, but one-per-email keeps threads, echoes,
  and audits 1:1.
- Use descriptive filenames; the original filename rides the manifest, the
  audit chain, and the echo report.
- Text-based PDFs (not scans) get the fastest, cheapest handling — see the
  capability handoff below.
- Keep documents within the free triage input budget (~12,000 characters of
  text) when you want the $0 lane to finish them; longer documents are
  automatically handed to the full paid pipeline.
- Don't re-send a document "to try again" — every email is a new document.
  Recover parked documents from the review/failed bins (or the smoke-test
  tooling) instead; the Message-ID dedup prevents double-queuing of the
  same email, not of re-sent copies.

---

## 3. The journey — every pathway from Gmail into the pipeline

### Common path (all documents)

```
 email arrives at llmmailroom@gmail.com
        │
        ▼
 IMAP sweep (every MAILROOM_GMAIL_POLL_SECONDS, UNSEEN only)
   1. sender allowlist check          → rejected senders marked seen, skipped
   2. count ACCEPTED attachments      → extension + size filters
   3. route stamp on every sidecar:
        exactly ONE accepted  → route: triage
        TWO or MORE accepted  → route: pipeline
        zero accepted         → marked seen, skipped (logged)
   4. attachments → SAME inbox bin the watcher drains
      + /upload <file>.meta sidecar (source, message_id, sender,
        subject, matter_id, route, upload_id, size, received_at)
   5. message marked \Seen; Message-ID recorded in
      <base>/gmail_intake_state.json (a lost seen-mark can never double-queue)
        │
        ▼
 Watcher claim (watcher.lock — single intake authority)
   file → processing/<worker_id>/, matter_id + intake meta resolved
   ✅ reaction applied to the source email (Gmail label via IMAP
      X-GM-LABELS, RFC 3501 modified-UTF-7; one per Message-ID;
      best-effort — a failure never disturbs the claim)
        │
        ▼
 ROUTE DECISION  ──────────────┬──────────────────────────────
        │                      │
 route: triage          route: pipeline  (also: triage handoff, triage off)
        │                      │
 PATHWAY A              PATHWAY B / C below
```

From here the document follows one of three pathways. **All pathways end in
a terminal manifest** (archived / review / failed) that dispatches the
completion echo on the source email thread.

### Pathway A — single-document upload → the FREE triage lane

One accepted attachment per email (`route: triage`) and
`MAILROOM_GMAIL_TRIAGE` on (the default with the channel).

```
 capability pre-check (deterministic, LLM-free — no doomed runs)
   pass → free triage lane
        ▼
 deterministic prep     doc text read (pdfplumber/pypdf/docx/plain) +
 (never an LLM)         apply_intake normalization
        ▼
 triage read            GmailTriageAgent (z-ai/glm-5.2:free, $0):
 (advisory)             primary_doc_class + doc_subclass + confidence +
                        one-sentence gist + ≤6 keywords;
                        validate_triage clamps to the live taxonomy
        ▼
 auditable-hash archive triage_ingested / triage_classified /
                        triage_archived audit entries (OWN section — never
                        conflated with the pipeline's ingested/classified/
                        extracted/archived vocabulary) → file + manifest +
                        JSON sidecar into archive/<matter>/<doc_type>/
        ▼
 terminal manifest (ARCHIVED) → completion echo on the source thread
```

- **No paid agent is ever called.** The lane performs the core steps of the
  full pipeline — deterministic preparation, classification, auditable-hash
  archive, completion echo — without the paid agents.
- **Advisory by design:** the triage read is the accurate intake log, never
  the final word. It never overrules pipeline agents (it only exists where
  no pipeline run happens).
- **Fail-soft:** no `OPENROUTER_API_KEY`, a rate limit, or a provider error
  never blocks intake — the document parks to `failed/` (abort path) and
  the echo reports it.

### Pathway B — capability handoff → the full paid pipeline

The pre-check (`watcher.py:_triage_capability_check`) rejects documents
beyond the free team's reach and **honestly hands them off** — no doomed
runs, the sender is told exactly why:

| Handoff reason | Meaning |
|:---|:---|
| `image_requires_vision` | Image-only input (.jpg/.png/...) — needs the paid vision path |
| `scanned_pdf_requires_transcription` | PDF with no direct text layer (scanned) — needs the paid transcriber |
| `exceeds_free_budget:N>M` | Text longer than the `gmail_triage` `max_input_chars` budget (default 12,000 chars) — **merger agreements typically exceed it** |
| `no_extractable_text` / `unreadable` | No text could be extracted deterministically |

The handoff reason rides `intake.triage_handoff` onto the terminal manifest
and the echo renders it: `triage handoff: <reason> — handled by the full
pipeline`. The document then follows **Pathway C** exactly.

### Pathway C — multi-document email (or handoff) → the FULL paid pipeline

Two or more accepted attachments (`route: pipeline`; triage dropped), any
document after a Pathway-B handoff, and everything when
`MAILROOM_GMAIL_TRIAGE=0` runs the standard 13-node graph **per document**:

```
 ingest (transcription: direct PDF text / paid LLM transcription for scans /
         vision OCR for images + intake normalization)
   → classify (sorter; retry + independent sorter-reviewer lanes)
   → extract (doc-class specialist; handoff context; retry lane)
   → judge_verify + arbiter (ambiguous-band completeness gate)
   → compile_report (procedural) → catalog_write
   → archive (hash-chained audit entry)
   → terminal manifest (ARCHIVED / REVIEW / FAILED)
```

Every attachment of the same email becomes its own document under the same
matter, with `intake.source: gmail` + `intake.route: pipeline` + sender/
subject/message_id recorded on the manifest for audit; live traces are
tagged `source-gmail`. Terminal documents get the completion report on the
thread: STATUS, doc_id/matter, classification + confidence, the extraction
report, the archive entry (path + sha256) or the failure/review reason, and
the full audit trail with the hash-chain verification verdict.

### Terminal outcomes & the completion echo

| Terminal stage | What happened | Sender sees |
|:---|:---|:---|
| `ARCHIVED` | Processed and sealed in the auditable hash archive | Report + archive path + sha256 |
| `REVIEW` | Parked in the human-review queue (low confidence, guardrails, escalation) | Report + the reason |
| `FAILED` | Pipeline error / fail-soft triage failure | Report + the error reason |

The echo (`send_intake_echo`) replies **on the source thread**
(`In-Reply-To`/`References` headers, To: the original sender, subject
`Re: <original>`) with:

1. **STATUS** + document/doc_id/matter/received-via lines
2. **CLASSIFICATION** (doc_type, subclass, confidence)
3. **INTAKE TRIAGE (pre-pipeline)** — the free-team read, when the triage
   lane ran
4. **triage handoff** line — when the capability pre-check routed the
   document to the full pipeline
5. **EXTRACTION** — the specialist report + extracted fields
6. **ARCHIVE ENTRY** — path + sha256 (+ chain hashes), or why it was not
   archived
7. **AUDIT TRAIL** — every event with actor + timestamp, and the hash-chain
   verification verdict (`OK — hash chain intact` / `BROKEN — investigate
   immediately`)

Echoes are deduped per `(doc_id, stage)`; a failed send is retried by the
next terminal event; `MAILROOM_GMAIL_ECHOES=0` disables. A triage-lane
document whose claim-time ✅ reaction failed gets the reaction retried at
echo time (it has exactly one claim, so this is the last chance to ack).

### Quick pathway reference

| You send | Pathway | Cost | Ends in |
|:---|:---|:---|:---|
| 1 text-based attachment, ≤ free budget | A — free triage lane | $0 | archived (or failed, fail-soft) |
| 1 image-only / scanned / over-budget attachment | B — honest handoff → C | paid | full-pipeline terminal stages |
| 2+ accepted attachments | C — full paid pipeline per attachment | paid | full-pipeline terminal stages |
| No accepted attachment | — | — | marked seen, skipped (no document) |
| Rejected sender | — | — | marked seen, skipped (no document) |
| Body-only email | — | — | marked seen, skipped (no document) |

---

## 4. Operations & troubleshooting

**Monitoring**

- `/health` → `checks.gmail_intake`: `enabled`, `running`, `last_poll_at`,
  and counters (`messages_seen`, `attachments_queued`, `reactions_sent`,
  `reactions_failed`, `echoes_sent`). Credentials never appear.
- Logs: structlog events `gmail_poller_started`, `gmail_attachment_queued`
  (with `route=`), `gmail_message_sender_rejected`,
  `gmail_message_no_processable_attachments`, `triage_handoff`,
  `file_claimed_triage`, `gmail_echo_sent`, `gmail_echo_failed`.
- State file: `<MAILROOM_BASE_DIR>/gmail_intake_state.json` — the bounded
  (2,000-entry) Message-ID ledger that makes double-queuing impossible even
  if a `\Seen` mark is lost.

| Symptom | Cause | Fix |
|:---|:---|:---|
| `checks.gmail_intake.enabled: false` | Opt-in or credentials missing | Set `MAILROOM_GMAIL_ENABLED=1` + `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` in `.env`; restart the watcher |
| Email never picked up | Not unseen / no accepted attachment / sender rejected / already processed | Check the skip counters + `gmail_message_*` log events; confirm the attachment extension + size |
| No ✅ reaction | Best-effort reaction failed (network/IMAP) | Retry happens on the next claim or at echo time; `reactions_failed` counter exposes it; `MAILROOM_GMAIL_REACTIONS=0` disables entirely |
| Document parked in `review/` | Pipeline confidence/guardrails (normal) or fail-safe | Resolve via the review flow (The-Mailroom REVIEW or `POST /v1/review/{doc_id}/resolve`) |
| Document parked in `failed/` after triage | No `OPENROUTER_API_KEY` for the triage model, rate limit, or provider error | Add/verify the key; re-run from the failed bin — intake itself never crashes |
| Everything runs the paid pipeline | Triage disabled (`MAILROOM_GMAIL_TRIAGE=0`) or capability handoff | Expected for images, scans, over-budget text; check `intake.triage_handoff` on the manifest |
| Echo never arrives | Echo send failed (SMTP) | It retries on the next terminal event; check `gmail_echo_failed` logs + `MAILROOM_GMAIL_SMTP_*` |
| Two watchers fight over the inbox | A second drain process exists | Only one `watcher.lock` holder; the Gmail poller always runs inside it |

**Disablement matrix**

| Flag `=0` | Effect |
|:---|:---|
| `MAILROOM_GMAIL_ENABLED` | Channel fully off (poller never starts) |
| `MAILROOM_GMAIL_TRIAGE` | Single-document emails take the full paid pipeline (Pathway C) |
| `MAILROOM_GMAIL_ECHOES` | No completion-report replies (reactions still fire) |
| `MAILROOM_GMAIL_REACTIONS` | No ✅ acknowledgement (echoes still fire) |
