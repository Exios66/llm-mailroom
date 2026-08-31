# Hosted Hugging Face Spaces pair

- **Date**: 2026-08-30
- **Kind**: pilots (Pilot-run evaluation write-ups)
- **Status**: complete (Observatory live; producer Space not published)
- **Hub pair**:
  - Observatory: [`Lucius-Morningstar/mailroom-observatory`](https://huggingface.co/spaces/Lucius-Morningstar/mailroom-observatory)
  - Producer: [`Lucius-Morningstar/mailroom-producer`](https://huggingface.co/spaces/Lucius-Morningstar/mailroom-producer) — **not created** (Hub 404)

## Scope

Pilot the two Hugging Face Docker Spaces that make the floor + inbox +
REVIEW desk reachable from the Hub after The-Mailroom
[PR #30](https://github.com/Exios66/The-Mailroom/pull/30) and llm-mailroom
[#54](https://github.com/Exios66/llm-mailroom/pull/54) /
[#55](https://github.com/Exios66/llm-mailroom/pull/55):

| Space | Process | Expected host |
|---|---|---|
| **Observatory** (floor) | `python -m server.hosted` | `https://lucius-morningstar-mailroom-observatory.hf.space` |
| **Producer** (pipeline) | `python -m api.main` | `https://lucius-morningstar-mailroom-producer.hf.space` |

This write-up is a **hosted** pass: Hub cards, the live `.hf.space` UI,
and Observatory HTTP contracts. It is not a `run_pilot.py --real` spend
(no `OPENROUTER_API_KEY` / `HF_TOKEN` in this agent environment).

## Method

1. Hub API + public HTTP against both Space ids and both `.hf.space` hosts.
2. Observatory JSON: `/api/health`, `/api/pipeline`, `/api/review-queue`,
   `/api/traces`, `/api/sessions`, `/api/metrics`, `/api/inbox/enqueue`,
   `/api/review/resolve`.
3. Browser QA of the **actual hosted UI** (not localhost): Hub card,
   `/live` Pipeline / Review / History / Matters / Metrics / Debug, plus
   the producer Hub URL.
4. Network-free repo checks: `publish_space.py --check`,
   `run_hf_pilot.py --check`.

## Findings

### 1. Observatory Space is Running and updated (PR #30)

Hub runtime (probed 2026-08-30T00:30Z):

| Field | Value |
|---|---|
| Id | `Lucius-Morningstar/mailroom-observatory` |
| SDK | docker, `app_port` 7860 |
| Stage | **RUNNING** (cpu-basic, 1 replica) |
| Host | `https://lucius-morningstar-mailroom-observatory.hf.space` |
| Last Space commit | `a137b58` — *Republish Observatory after #30 (cards, cache, inbox upload)* (2026-08-30T00:26:55Z) |
| App | hosted edition `0.3.13` (`/live`) |

`GET /api/health` → `ok: true`, `langfuse: true`, `source: langfuse`,
`pipeline_configured: false`. Cache: **74** traces. Metrics window:
74 docs, 45 archived, 27 review, 1 failed, 1 in flight; classes
contract 21 / insurance_claim 15 / correspondence 14 / corporate_record 12
/ merger_agreement 12.

The hosted desk shows the PR #30 surfaces: **Queue a document**, REVIEW
resolve panel, classification headlines (72/74 primary classified),
Export snapshot, Debug bundle.

### 2. Producer Space is not on the Hub

`https://huggingface.co/spaces/Lucius-Morningstar/mailroom-producer` is a
Hub **404**. `https://lucius-morningstar-mailroom-producer.hf.space/health`
is also 404. There is no public producer for the Observatory to call.

This environment has **no `HF_TOKEN`**, so `publish_space.py` cannot
create the Space or write secrets. Hugging Face MCP auth is desktop-only.

### 3. Honest 503s — inbox / REVIEW do not fabricate a catalog

| Call | Result |
|---|---|
| `GET /api/pipeline` | `{"configured":false,"ok":null,"watcher":"unconfigured"}` |
| `POST /api/inbox/enqueue` (multipart file) | **503** `Set MAILROOM_PIPELINE_URL and MAILROOM_PIPELINE_TOKEN…` |
| `POST /api/review/resolve` `{"doc_id":"x","decision":"approved"}` | **503** `MAILROOM_PIPELINE_URL is not set` |

The Queue form and REVIEW pane render the same setup copy in the browser.
Display envelopes stay Langfuse-only — the floor is populated without a
producer.

### 4. Observatory mailroom pin is one merge behind this repo

Health `mailroom.pin` is `0928de1` (pre [#54](https://github.com/Exios66/llm-mailroom/pull/54)
/ [#55](https://github.com/Exios66/llm-mailroom/pull/55)). Floor display
does not need that pin; pairing + `/v1/upload` do. Republish the
Observatory **after** the producer exists and set the three knobs.

### 5. Repo publisher payload still checks clean

```bash
PYTHONPATH=src python src/scripts/publish_space.py --check
PYTHONPATH=src python src/scripts/run_hf_pilot.py --check
```

`--check` is the network-free HF-pilot + intake contract. A live
`--real` producer pilot needs `HF_TOKEN` + `MAILROOM_API_TOKEN` +
(for extract) `OPENROUTER_API_KEY`.

## Recommendations

1. From a laptop with Hub write access (org `Lucius-Morningstar`):

   ```bash
   export MAILROOM_API_TOKEN="$(openssl rand -hex 24)"
   HF_TOKEN=hf_... MAILROOM_API_TOKEN=$MAILROOM_API_TOKEN \
     OPENROUTER_API_KEY=sk-or-... \
     LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
     PYTHONPATH=src python src/scripts/publish_space.py \
       --repo Lucius-Morningstar/mailroom-producer
   ```

   Keep the Space **public**. Then on The-Mailroom:

   ```bash
   export MAILROOM_PIPELINE_URL=https://lucius-morningstar-mailroom-producer.hf.space
   export MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
   export MAILROOM_PIPELINE_API_PREFIX=/v1
   python scripts/publish_space.py --repo Lucius-Morningstar/mailroom-observatory
   ```

2. Re-probe: Observatory `/api/pipeline` must show `configured: true` and
   a watcher lamp; Inbox Queue must accept a file → producer `202`.
3. Do not put tokens in Space **Variables**.
4. Treat Space `/data` as ephemeral.

## Hosted demo

Screenshots and a walkthrough of the **live** Observatory (plus the
honest producer 404) are attached to the pull request — they are the
actual Hub / `.hf.space` UI from 2026-08-30, not a local stand-in.
