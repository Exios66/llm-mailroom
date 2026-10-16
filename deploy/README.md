<div align="center">

# 🚀 Deployment Configuration

**Compose files, Modal app, and Space payload for llm-mailroom.**

</div>

---

Operator manuals for this folder are published on the GitBook site (pages listed in [`docs/SUMMARY.md`](../docs/SUMMARY.md) — a page not listed there is not on the site):

| Guide | Covers |
|:---|:---|
| [Docker deployment](../docs/pipeline-reference-llm-mailroom/deployment/docker-deployment.md) | Compose matrix (Modes B / Mixed / A-ollama / A-llamafile / M / G), producer image, BERT build args |
| [Modal + vLLM](../docs/pipeline-reference-llm-mailroom/deployment/modal-vllm.md) | `mailroom-vllm` serve, pipeline cutover, multi-tier Mode G GPUs |
| [Deployment](../docs/pipeline-reference-llm-mailroom/deployment/README.md) | Laptop Python install, Railway, Hugging Face Spaces, backup |

This README is an index of files in `deploy/`. Keep the operator recipes on the GitBook pages so they stay in sync.

## Structure

| Path | Contents |
|:---|:---|
| [`space/`](space/) | HuggingFace Space deployment |
| `modal_vllm.py` | Modal + vLLM serving app (`mailroom-vllm`) |
| `docker-compose.yml` | Base compose — `app` + optional `watcher` (Modes A / B / mixed / M) |
| `docker-compose.ollama.yml` | Mode A overlay: local Ollama sidecar |
| `docker-compose.llamafile.yml` | Mode A overlay: local llamafile sidecar |
| `docker-compose.full.yml` | Mode G — app, ops-monitor, watchdog, postgres, LiteLLM gateway, optional Phoenix |
| `docker-compose.producer.yml` | The-Mailroom REVIEW pairing (reachable producer) |
| `llamafile/` | llamafile sidecar image (pinned binary) + entrypoint |
| `models/` | Host staging for weights (GGUF / ModernBERT bundle) — never committed |

`MAILROOM_API_TOKEN` is **required** on every compose `app` service (the container binds `0.0.0.0`; audit L-2 refuses an off-loopback bind without a live bearer token). Secrets come from the host `.env` via `--env-file`. Never bake tokens into images.

Quick start (Mode B / Mixed): see [Docker deployment](../docs/pipeline-reference-llm-mailroom/deployment/docker-deployment.md). GPU serve: see [Modal + vLLM](../docs/pipeline-reference-llm-mailroom/deployment/modal-vllm.md).

## Configuration Files

| File | Purpose |
|:---|:---|
| `Dockerfile` (repository root) | Multi-stage producer / compose image |
| `nixpacks.toml` | Nixpacks fallback |
| `railway.json` | Railway deployment config |

## Usage

Build from the package directory:

```bash
cd packages/llm-mailroom
docker build -t llm-mailroom .
```

(Standalone checkout: `docker build -t llm-mailroom .` at the repo root.)

## Operator SMS pings

Text alerts to the operator's phone: a digest every 10 minutes while the mailroom runs, plus an alert only when something needs attention (healthy 60-second scans stay silent). **Off by default.** Config lives in the `pager:` block of `src/config/taxonomy.yaml`; `python -m pipeline.pager status --full` shows every resolved key and where it came from.

Quick start:

1. Put Twilio credentials in `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID`.
2. `PYTHONPATH=src python -m pipeline.pager on --to +16085551234` (a 10-digit US number is normalized to E.164).
3. `PYTHONPATH=src python -m pipeline.pager test` sends `✅ MAILROOM PAGER TEST`; it works while alerts are off.
4. `PYTHONPATH=src python -m pipeline.pager preview` prints the digest the phone would get (`--alert`, `--json`); it never sends.
5. Start the mailroom. The watchdog hosts the sender (`MAILROOM_WATCHDOG=1` in compose), or run `pager run` in the foreground. One sender per data dir (`pager.lock`).

| Recipient | Transport | Notes |
|:---|:---|:---|
| Your mobile number | `twilio` (default) or `email_gateway` | Email gateway (`5551234567@vtext.com` style) reuses the Gmail SMTP settings; free but carriers throttle it. |
| Your Google Voice number | `twilio` only | Google Voice receives texts from a long-code or toll-free number, never from short codes, and has no email gateway. |

- **Twilio trial accounts** only text verified numbers: verify each recipient (mobile and Google Voice) under Phone Numbers → Verified Caller IDs.
- **US sending** from a long code needs A2P 10DLC registration; a verified toll-free number is the faster path for a single operator.
- **Cost:** emoji texts are UCS-2 (70 characters per segment). `style: ascii` keeps them GSM-7 (160 per segment), about 2.3× fewer segments. `max_chars` (default 640) caps each text; overflow lines go to the pager log.

**Start prompt.** On a live-production TTY start (`python -m api.main`, `python -m pipeline.watcher`) the operator is asked whether to enable text alerts and for a number (30-second timeout per question; the default leaves alerts as they are). `--pager` / `--no-pager` set the toggle without asking; `MAILROOM_PAGER_PROMPT=0` turns the prompt off. Non-interactive starts (docker, systemd, CI) log `pager_prompt_skipped` with the enable hint. The answer persists in `<base_dir>/pager_settings.json`, which wins over env for `enabled`/`recipients`; a running pager picks up `pager on|off` and `pager number …` within one scan.

**Machine-readable copy.** Each text ends with `Ref P-<HHMM>-<hex>`. The same facts as JSON (`schema: mailroom.pager/v1`, with masked delivery results) go to `<base_dir>/pager_log.jsonl` (`pager log --json`, rotates at 5 MB) and, when `MAILROOM_PAGER_WEBHOOK_URL` is set, are POSTed there (`X-Mailroom-Signature: sha256=…` with `MAILROOM_PAGER_WEBHOOK_SECRET`).

**What texts.** The watcher stopping, a Modal tier or OpenRouter down, a provider degraded, low OpenRouter credit, failed or stuck documents, the review backlog, an ingestion pause, undeliverable echo emails, and a stalled research run (`run_pilot.py` / `run_experiment.py` report progress; `--page` starts a sender for the run). Modal is never probed: status comes from passive LLM outcomes plus `modal container list`. Cooldowns, debounce, quiet hours (`quiet_hours.window: "22:00-07:00"`), the hourly cap and `pager mute 2h` are all in the `pager:` block. Only an external host (the watchdog, or `pager run`) can text "watcher stopped" when the watcher process itself dies.

## Related Files

- [`docs/docker-deployment.md`](../docs/pipeline-reference-llm-mailroom/deployment/docker-deployment.md)
- [`docs/modal-vllm.md`](../docs/pipeline-reference-llm-mailroom/deployment/modal-vllm.md)
- [`docs/deployment.md`](../docs/pipeline-reference-llm-mailroom/deployment/README.md)
- `src/` — Source code
