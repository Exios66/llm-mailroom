import { useEffect, useRef, useState } from "react";

import { fetchAuditDocument, fetchAuditDocuments } from "../api";
import type { AuditAddendum, AuditAttempt, AuditDocumentDetail, AuditDocumentList, AuditEntry } from "../types";

interface AuditPanelProps {
  followedDocId: string | null;
  refreshKey: number;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "The audit request failed.";
}

function stamp(value?: string | null): string {
  return typeof value === "string" && value ? value : "Unknown time";
}

function hashAnchor(hash: string): string {
  return `audit-hash-${hash.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

function HashLink({ hash, label, reference = false }: { hash?: string | null; label: string; reference?: boolean }) {
  if (!hash) return <span>—</span>;
  const anchor = hashAnchor(hash);
  return <a className="audit-hash" id={reference ? undefined : anchor} href={`#${anchor}`} aria-label={`${label} ${hash}`} title={hash}>{hash}</a>;
}

function EntryCard({ entry, title }: { entry: AuditEntry; title: string }) {
  return (
    <li className="audit-entry">
      <h4>{title}</h4>
      <dl className="facts audit-entry__facts">
        <dt>Entry ID</dt><dd>{entry.entry_id ?? "—"}</dd>
        <dt>Event</dt><dd>{entry.event ?? "—"}</dd>
        <dt>Actor</dt><dd>{entry.actor ?? "—"}</dd>
        <dt>Time</dt><dd>{stamp(entry.timestamp)}</dd>
        <dt>Previous hash</dt><dd><HashLink hash={entry.prev_hash} label="Previous hash" reference /></dd>
        <dt>Entry hash</dt><dd><HashLink hash={entry.entry_hash} label="Entry hash" /></dd>
      </dl>
      {entry.decision_fields?.length ? <p className="muted">Recorded fields: {entry.decision_fields.join(", ")}</p> : null}
    </li>
  );
}

function AttemptCard({ attempt }: { attempt: AuditAttempt }) {
  return (
    <li className="audit-subrecord">
      <strong>{attempt.stage ?? "Attempt"} {attempt.attempt_no ?? ""}</strong>
      <span>{attempt.outcome ?? attempt.trigger ?? "Recorded"}</span>
      <span>{stamp(attempt.started_at)}{attempt.ended_at ? ` → ${attempt.ended_at}` : ""}</span>
      <span>ID: {attempt.attempt_id ?? "—"}</span>
      <span>Hash: <HashLink hash={attempt.entry_hash} label="Attempt hash" /></span>
      {attempt.decision_fields?.length ? <span>Fields: {attempt.decision_fields.join(", ")}</span> : null}
    </li>
  );
}

function AddendumCard({ addendum }: { addendum: AuditAddendum }) {
  return (
    <li className="audit-entry">
      <h4>{addendum.kind ?? "Addendum"} · {addendum.addendum_id ?? "—"}</h4>
      <dl className="facts audit-entry__facts">
        <dt>Actor</dt><dd>{addendum.actor ?? "—"}</dd>
        <dt>Time</dt><dd>{stamp(addendum.timestamp)}</dd>
        <dt>Previous hash</dt><dd><HashLink hash={addendum.prev_hash} label="Addendum previous hash" reference /></dd>
        <dt>Entry hash</dt><dd><HashLink hash={addendum.entry_hash} label="Addendum hash" /></dd>
      </dl>
      {addendum.decision_fields?.length ? <p className="muted">Recorded fields: {addendum.decision_fields.join(", ")}</p> : null}
      {addendum.attempts?.length ? <><h5>Addendum attempts ({addendum.attempts.length})</h5><ul className="audit-subrecords" aria-label={`Addendum attempts (${addendum.attempts.length})`}>{addendum.attempts.map((attempt, index) => <AttemptCard key={attempt.attempt_id ?? index} attempt={attempt} />)}</ul></> : null}
    </li>
  );
}

function RecordView({ detail }: { detail: AuditDocumentDetail }) {
  const { record, verification } = detail;
  const unavailable = record.state === "unavailable" || detail.store?.state === "unavailable";
  const statusClass = verification.status === "verified" ? "verified" : verification.status === "failed" ? "failed" : verification.status === "unavailable" ? "unavailable" : "unverified";
  const events = record.events ?? [];
  const entries = [...events, ...detail.legacy.entries];
  const hasRow = record.state === "unified-row";
  const rowAttempts = record.attempts ?? [];
  const truncated = verification.truncated || detail.legacy.truncated;

  return (
    <div className="audit-record">
      <div className={`audit-status audit-status--${statusClass}`} role="status">
        <strong>{unavailable ? "Audit store unavailable" : record.state === "not-recorded" ? "No audit record" : `Verification ${verification.status}`}</strong>
        <span>{verification.method ? `Method: ${verification.method}` : detail.store?.reason ?? record.reason ?? "No verification method available"}</span>
      </div>
      <dl className="facts audit-summary">
        <dt>Schema</dt><dd>{detail.store?.schema ?? "—"}</dd>
        <dt>Record</dt><dd>{record.state}{record.status ? ` · ${record.status}` : ""}</dd>
        <dt>Checked</dt><dd>main {verification.checked.main} · attempts {verification.checked.attempts} · addenda {verification.checked.addenda} · legacy {verification.checked.legacy}</dd>
        <dt>Failures</dt><dd>{verification.failures.length}{verification.failures.length ? ` · ${verification.failures.map((failure) => [failure.scope, failure.reason].filter(Boolean).join(": ")).join(", ")}` : ""}</dd>
      </dl>
      <p className="muted audit-limits">Read limits: {Object.entries(detail.limits).map(([key, value]) => `${key} ${value}`).join(" · ") || "not reported"}</p>
      {truncated ? <p className="audit-warning" role="alert">This bounded audit read was truncated. Verification is not complete.</p> : null}
      {record.state === "legacy-events" ? <p className="muted">Only migrated legacy events were found; there is no current audit row.</p> : null}
      {record.state === "not-recorded" ? <p className="muted">No audit rows were found for this document id.</p> : null}
      {hasRow ? <ul className="audit-entries"><EntryCard title="Current audit row" entry={{ entry_id: record.entry_id, event: record.event, actor: record.actor, timestamp: record.timestamp, seq: record.seq, prev_hash: record.prev_hash, entry_hash: record.entry_hash, decision_fields: record.decision_fields }} /></ul> : null}
      {rowAttempts.length || (record.attempts_total ?? 0) > 0 ? <section aria-labelledby="audit-attempts-heading"><h3 id="audit-attempts-heading">Row attempts ({record.attempts_total ?? rowAttempts.length})</h3><ul className="audit-subrecords">{rowAttempts.map((attempt, index) => <AttemptCard key={attempt.attempt_id ?? index} attempt={attempt} />)}</ul></section> : null}
      {record.addenda?.length ? <section aria-labelledby="audit-addenda-heading"><h3 id="audit-addenda-heading">Addenda</h3><ul className="audit-entries">{record.addenda.map((item, index) => <AddendumCard key={item.addendum_id ?? index} addendum={item} />)}</ul></section> : null}
      {entries.length ? <section aria-labelledby="audit-events-heading"><h3 id="audit-events-heading">Events ({record.entries ?? entries.length})</h3><ol className="audit-entries">{entries.map((entry, index) => <EntryCard key={entry.entry_id ?? `${entry.source}-${index}`} title={`${entry.source === "legacy" ? "Legacy event" : "Event"} ${entry.seq ?? index + 1}`} entry={entry} />)}</ol></section> : null}
      {verification.notes.map((note, index) => <p className="muted" key={`verification-${index}`}>{note}</p>)}
      {detail.notes.map((note, index) => <p className="muted" key={`note-${index}`}>{note}</p>)}
    </div>
  );
}

export function AuditPanel({ followedDocId, refreshKey }: AuditPanelProps) {
  const [followRun, setFollowRun] = useState(true);
  const [manualDocId, setManualDocId] = useState("");
  const [lookup, setLookup] = useState("");
  const [listing, setListing] = useState<AuditDocumentList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listOffset, setListOffset] = useState(0);
  const [detailRefreshVersion, setDetailRefreshVersion] = useState(0);
  const [record, setRecord] = useState<{ docId: string; generation: string; detail: AuditDocumentDetail } | null>(null);
  const [recordError, setRecordError] = useState<{ docId: string; generation: string; message: string } | null>(null);
  const [recordLoading, setRecordLoading] = useState<{ docId: string; generation: string; loading: boolean } | null>(null);
  const recordSequence = useRef(0);
  const listSequence = useRef(0);
  const inFlightRecord = useRef<{ docId: string; controller: AbortController } | null>(null);
  const generationRef = useRef("");
  const activeDocId = (followRun ? followedDocId : manualDocId.trim()) || "";
  const detailGeneration = `${refreshKey}:${detailRefreshVersion}`;

  async function refreshList(signal?: AbortSignal) {
    const sequence = ++listSequence.current;
    setListLoading(true);
    try {
      const result = await fetchAuditDocuments(25, listOffset, signal);
      if (sequence !== listSequence.current || signal?.aborted) return;
      setListing(result);
      setListError(null);
    } catch (error) {
      if (sequence !== listSequence.current || signal?.aborted) return;
      setListError(message(error));
    } finally {
      if (sequence === listSequence.current && !signal?.aborted) setListLoading(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void refreshList(controller.signal);
    return () => { controller.abort(); listSequence.current += 1; };
  }, [listOffset]);

  function loadRecord(docId: string): void {
    const request = ++recordSequence.current;
    const controller = new AbortController();
    inFlightRecord.current = { docId, controller };
    setRecordLoading({ docId, generation: generationRef.current, loading: true });
    void fetchAuditDocument(docId, controller.signal).then((detail) => {
      if (controller.signal.aborted || request !== recordSequence.current) return;
      // A coalesced poll may have advanced the generation while this read was
      // in flight; the payload is still a current read of the same document.
      const generation = generationRef.current;
      setRecord({ docId, generation, detail });
      setRecordError(null);
      setRecordLoading({ docId, generation, loading: false });
    }).catch((error: unknown) => {
      if (controller.signal.aborted || request !== recordSequence.current) return;
      const generation = generationRef.current;
      // A failed refresh must not leave an older verification verdict on
      // screen as if it described this read.
      setRecord(null);
      setRecordError({ docId, generation, message: message(error) });
      setRecordLoading({ docId, generation, loading: false });
    }).finally(() => {
      if (inFlightRecord.current?.controller === controller) inFlightRecord.current = null;
    });
  }

  useEffect(() => {
    generationRef.current = detailGeneration;
    const inFlight = inFlightRecord.current;
    if (!activeDocId) {
      inFlight?.controller.abort();
      inFlightRecord.current = null;
      recordSequence.current += 1;
      setRecord(null);
      setRecordError(null);
      setRecordLoading(null);
      return;
    }
    if (inFlight && inFlight.docId === activeDocId) {
      // The 15s poll and the manual refresh both bump refreshKey. Coalesce that
      // tick onto the read already in flight instead of aborting and
      // restarting it: a response slower than the poll interval would otherwise
      // never land. Selection changes still abort below.
      return;
    }
    inFlight?.controller.abort();
    inFlightRecord.current = null;
    recordSequence.current += 1;
    setRecord(null);
    setRecordError(null);
    loadRecord(activeDocId);
  }, [activeDocId, detailGeneration]);

  useEffect(() => () => {
    inFlightRecord.current?.controller.abort();
    inFlightRecord.current = null;
  }, []);

  // Identity is the document, not the refresh generation: a coalesced refresh
  // keeps the last read visible and the loading status honest while the
  // in-flight request finishes. A different document never leaks through.
  const currentRecord = record?.docId === activeDocId ? record.detail : null;
  const currentError = recordError?.docId === activeDocId ? recordError.message : null;
  const currentLoading = recordLoading?.docId === activeDocId && recordLoading.loading;
  const notAvailableForRun = followRun && !followedDocId;

  return (
    <section className="panel audit-panel" aria-label="Audit record" id="audit-panel" tabIndex={-1}>
      <div className="audit-panel__heading">
        <h2>Audit record</h2>
        <a className="audit-top-link" href="#audit-panel">Audit details</a>
      </div>
      <label className="audit-follow">
        <input type="checkbox" checked={followRun} onChange={(event) => setFollowRun(event.target.checked)} />
        Follow selected run document
      </label>
      {followRun ? <p className="muted">{followedDocId ? `Following run document ${followedDocId}` : "Select a run with a document id to load its audit record."}</p> : (
        <>
          <label className="run-panel__label" htmlFor="audit-document-select">Choose a listed document</label>
          <select id="audit-document-select" value={manualDocId} onChange={(event) => setManualDocId(event.target.value)}>
            <option value="">Select audit document</option>
            {manualDocId && !listing?.documents.some((document) => document.doc_id === manualDocId) ? <option value={manualDocId}>Manual lookup · {manualDocId}</option> : null}
            {listing?.documents.map((document) => <option key={document.doc_id} value={document.doc_id}>{document.doc_id}{document.event ? ` — ${document.event}` : ""}</option>)}
          </select>
          <form className="audit-lookup" onSubmit={(event) => { event.preventDefault(); setManualDocId(lookup.trim()); }}>
            <label className="run-panel__label" htmlFor="audit-document-lookup">Look up document id</label>
            <input id="audit-document-lookup" value={lookup} onChange={(event) => setLookup(event.target.value)} autoComplete="off" />
            <button className="button" type="submit" disabled={!lookup.trim()}>Look up</button>
          </form>
        </>
      )}
      <div className="audit-list-controls">
        <span>{listing?.store.state === "unavailable" ? `List unavailable: ${listing.store.reason ?? "unknown reason"}` : listing ? `${listing.pagination.returned} documents · page offset ${listing.pagination.offset}` : "Document list not loaded"}</span>
        <div className="audit-list-controls__actions">
          <button className="button" type="button" onClick={() => { setDetailRefreshVersion((version) => version + 1); void refreshList(); }} disabled={listLoading}>Refresh audit and list</button>
          <button className="button" type="button" onClick={() => setListOffset((offset) => Math.max(0, offset - 25))} disabled={listLoading || listOffset === 0}>Previous page</button>
          <button className="button" type="button" onClick={() => setListOffset((offset) => offset + 25)} disabled={listLoading || !listing?.pagination.more}>Next page</button>
        </div>
      </div>
      {listError ? <p className="audit-warning" role="alert">{listError}</p> : null}
      {listing?.store.state === "unavailable" ? <p className="muted">The audit store could not be read. Check the producer connection and database setup.</p> : null}
      {notAvailableForRun ? <p className="muted">The selected journal does not contain a document id for audit lookup.</p> : null}
      {activeDocId && currentLoading ? <p role="status">Loading audit for {activeDocId}…</p> : null}
      {activeDocId && currentError ? <p className="audit-warning" role="alert">Audit lookup failed for {activeDocId}: {currentError}</p> : null}
      {currentRecord ? <RecordView detail={currentRecord} /> : null}
    </section>
  );
}
