import type { ExperimentDetail, ExperimentSummary } from "../types";

interface ExperimentPanelProps {
  experiments: ExperimentSummary[];
  selectedId: string;
  detail: ExperimentDetail | null;
  onSelect: (id: string) => void;
  onSelectRun: (executionId: string) => void;
}

function metric(value: unknown): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value === null || value === undefined) return "—";
  return String(value);
}

export function ExperimentPanel({
  experiments,
  selectedId,
  detail,
  onSelect,
  onSelectRun,
}: ExperimentPanelProps) {
  const revision =
    detail && typeof detail.corpus.revision === "object" && detail.corpus.revision !== null
      ? String((detail.corpus.revision as Record<string, unknown>).tag ?? "")
      : "";
  return (
    <section className="panel experiment-panel" aria-label="Experiment logs">
      <h2>Experiments</h2>
      <label className="run-panel__label" htmlFor="experiment-select">
        Load an experiment log
      </label>
      <select
        id="experiment-select"
        value={selectedId}
        onChange={(event) => onSelect(event.target.value)}
      >
        <option value="">No experiment loaded</option>
        {experiments.map((experiment) => (
          <option key={experiment.experiment_id} value={experiment.experiment_id}>
            {experiment.experiment_id} — {experiment.mode ?? "?"} · {experiment.runs} runs
          </option>
        ))}
      </select>

      {experiments.length === 0 ? (
        <p className="muted">
          No experiment logs in this base directory. The live smoke runner writes one.
        </p>
      ) : null}

      {detail ? (
        <>
          <dl className="facts run-panel__facts">
            <dt>Corpus</dt>
            <dd>
              {detail.corpus.name ?? "?"}
              {revision ? ` · ${revision}` : ""}
            </dd>
            <dt>Mode</dt>
            <dd>{detail.mode ?? "?"}</dd>
            <dt>Runs</dt>
            <dd>{metric(detail.metrics.n)}</dd>
            <dt>Class / stage ok</dt>
            <dd>
              {metric(detail.metrics.class_ok)} / {metric(detail.metrics.stage_ok)}
            </dd>
            <dt>All checks</dt>
            <dd>{metric(detail.metrics.all_checks_ok)}</dd>
          </dl>
          <ul className="experiment-runs">
            {detail.runs.map((run) => (
              <li key={`${run.execution_id}-${run.manifest_id ?? ""}`}>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => onSelectRun(run.execution_id)}
                >
                  {run.manifest_id ?? run.filename ?? run.execution_id} — {run.expected} →{" "}
                  {run.stage ?? "?"}
                </button>
              </li>
            ))}
          </ul>
          <p className="caveat">
            Experiment logs are validity summaries over recorded journals; they do not replace the
            journal path or re-execute the pipeline.
          </p>
        </>
      ) : null}
    </section>
  );
}
