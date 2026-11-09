import type { RunJournal, RunSummary } from "../types";

interface RunPanelProps {
  runs: RunSummary[];
  selectedId: string;
  journal: RunJournal | null;
  step: number | null;
  playing: boolean;
  onSelect: (id: string) => void;
  onStep: (index: number) => void;
  onPlay: () => void;
  onStop: () => void;
  onReset: () => void;
}

export function RunPanel({
  runs,
  selectedId,
  journal,
  step,
  playing,
  onSelect,
  onStep,
  onPlay,
  onStop,
  onReset,
}: RunPanelProps) {
  const sequence = journal?.node_sequence ?? [];
  return (
    <section className="panel run-panel" aria-label="Recorded runs">
      <h2>Recorded runs</h2>
      <label className="run-panel__label" htmlFor="run-select">
        Load a run journal
      </label>
      <select
        id="run-select"
        value={selectedId}
        onChange={(event) => onSelect(event.target.value)}
      >
        <option value="">No run — topology only</option>
        {runs.map((run) => (
          <option key={run.execution_id} value={run.execution_id}>
            {run.document.filename ?? run.execution_id} — {run.outcome.doc_type ?? "?"} /{" "}
            {run.outcome.stage ?? "?"}
          </option>
        ))}
      </select>

      {runs.length === 0 ? (
        <p className="muted">
          No recorded runs in this base directory. Run{" "}
          <code>smoke_live_documents.py</code> to write a journal.
        </p>
      ) : null}

      {journal ? (
        <>
          <dl className="facts run-panel__facts">
            <dt>Document</dt>
            <dd>{journal.document.manifest_id ?? journal.document.filename ?? "unknown"}</dd>
            <dt>Outcome</dt>
            <dd>
              {journal.outcome.doc_type ?? "?"} → {journal.outcome.stage ?? "?"}
            </dd>
            <dt>Expected</dt>
            <dd>
              {String(journal.ground_truth?.expected_doc_class ?? "?")} →{" "}
              {String(journal.ground_truth?.expected_stage ?? "?")}
            </dd>
            <dt>Steps</dt>
            <dd>{sequence.length}</dd>
          </dl>
          <ol className="run-path" aria-label="Recorded node path">
            {sequence.map((node, index) => (
              <li key={`${node}-${index}`}>
                <button
                  type="button"
                  className={step === index ? "is-current" : undefined}
                  aria-current={step === index ? "step" : undefined}
                  onClick={() => onStep(index)}
                >
                  <span className="run-path__index">{index + 1}</span> {node}
                </button>
              </li>
            ))}
          </ol>
          <div className="run-panel__controls">
            <button className="button" type="button" onClick={playing ? onStop : onPlay}>
              {playing ? "Stop" : "Play path"}
            </button>
            <button className="button" type="button" onClick={onReset}>
              Show all
            </button>
          </div>
          <p className="caveat">
            Playback reads the recorded journal; it does not execute the pipeline. Re-run and diff
            an execution with{" "}
            <code>smoke_live_documents.py --replay &lt;journal&gt;</code>.
          </p>
        </>
      ) : null}
    </section>
  );
}
