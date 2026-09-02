# AgentMail integration evaluation

- **Date**: 2026-08-10
- **Kind**: evaluations (Offline judge/quality evaluations over corpora)
- **Status**: complete
- **Source**: [AgentMail blog — Give your LangChain agent a real inbox](https://www.agentmail.to/blog/give-your-langchain-agent-a-real-inbox), [AgentMail docs](https://docs.agentmail.to/integrations/langchain), [langchain-agentmail](https://github.com/agentmail-to/langchain-agentmail)

## Scope

Issue #3 asks whether AgentMail — an "inbox as an API" for agents, with an
official LangChain integration (`langchain-agentmail`) — should be integrated
into LLM-Mailroom. This evaluation assesses fit against the current
architecture and recommends a concrete integration path (or a deferral).

## Method

- Read the AgentMail LangChain integration docs (toolkit, loader/retriever,
  webhook FastAPI router).
- Mapped AgentMail's capabilities against Mailroom's existing input channels
  (filesystem watcher on `data/pipeline/inbox/`, `POST /upload` API, pilot
  corpus tooling) and its doc classes (including `correspondence`).
- Identified the minimal, non-invasive integration point consistent with the
  repo's design rules (files only move through `pipeline/bins.py`; one
  LangGraph run per document; guardrails before routing).

## Findings

1. **AgentMail's core value (receiving mail) is a genuinely missing input
   channel for Mailroom.** Today Mailroom ingests filesystem drops and API
   uploads. Real law-firm workflow includes email: clients email contracts,
   opposing counsel emails notices/demands, courts email orders. AgentMail
   gives agents a persistent address that can *receive*, which is exactly the
   half SendGrid/Mailgun don't cover.
2. **The LangChain integration maps cleanly onto the existing graph.** The
   toolkit's tools (create inbox, list/read threads, send, reply, draft,
   label) are standard LangChain tools; Mailroom already runs a
   LangGraph-based state machine, so the tools could be exposed to a future
   "email triage" agent without new infrastructure. The webhook router
   (`create_fastapi_router`) is a natural addition to `api/main.py`.
3. **The recommendable integration is a webhook → inbox-bin adapter, not a
   LangChain-toolkit agent.** The minimal path: AgentMail webhook
   (`message.received`) → extract attachments (PDFs, `.docx`, `.txt`) and the
   email body (as a `.txt`) → write files into `data/pipeline/inbox/` via the
   existing `pipeline/bins.py` helpers → the watcher picks them up with zero
   changes to the graph. This preserves the repo rule "files only move
   through `pipeline/bins.py`" and keeps all audit/tracing/guardrail
   machinery. A full "agent replies to email" loop is a separate, larger
   feature (see recommendation 4).
4. **Correspondence fit.** Inbound email bodies map naturally to the existing
   `correspondence` doc class (`sender`, `recipient`, `key_points`,
   `action_items`, `urgency`), so a received email body dropped as a `.txt`
   would be classified, extracted, and archived like any other document with
   no schema changes.
5. **Costs/risks.** New external dependency (`langchain-agentmail` +
   `AGENTMAIL_API_KEY`), a new always-on webhook endpoint, and the usual
   PII/confidentiality considerations (legal email content in transit + in
   Langfuse traces — the repo already treats trace content as deliberate).
   AgentMail is a commercial SaaS (SOC 2); a self-hosted fallback (IMAP +
   MIME parsing) is the repo's own `correspondence`-style plumbing and is
   exactly what AgentMail removes.

## Recommendations

1. **Adopt AgentMail as an input channel (not an agent)** via a small adapter
   module, e.g. `api/agentmail_webhook.py`: `message.received` → attachments
   + body → `pipeline/bins.py` inbox writes. This is ~50 lines, keeps the
   graph untouched, and reuses all existing quality machinery (classify →
   extract → report → archive, tracing, judges, field scoring).
2. **Add the webhook route behind an env flag** (`AGENTMAIL_WEBHOOK_SECRET`
   for signature verification; disabled by default) so the dependency is
   optional and the API stays single-responsibility.
3. **When wiring the body as a document**, prefer the existing
   `correspondence` doc class and let the sorter assign it; attachments stay
   as-is so the specialist pipeline (PDF transcription, vision) handles them
   unchanged.
4. **Defer the "agent sends/replies to email" loop.** The toolkit's send/draft
   tools are tempting but introduce an outbound side effect into a pipeline
   whose current contract is ingest→archive. Revisit after the inbound
   channel proves out (measure: email-originated documents reaching
   `archived` at the same quality as uploads).
5. **Track with the existing observability.** Email-originated traces should
   carry a `source-email` tag (or a `source: email` in trace metadata) so the
   new Langfuse dimension dashboards (issue #2) can segment email volume and
   quality without new instrumentation.

## Verification

- No code changes were made in this evaluation (assessment only, per issue #3's
  "Evaluate the integration" framing).
- When implemented: `python -m pytest tests/ -q` must stay green; a mock
  webhook payload test should assert the attachment lands in the inbox bin
  and the pipeline archives it (`--mock` path).
