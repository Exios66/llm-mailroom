import type { CheckReport } from "../types";

export function FindingsPanel({
  report,
  failure = null,
}: {
  report: CheckReport | null;
  failure?: string | null;
}) {
  if (!report) {
    return (
      <section className="panel findings" aria-label="Structural findings">
        <h2>Findings</h2>
        <p className="muted">
          {failure ?? "Findings are unavailable until the snapshot loads."}
        </p>
      </section>
    );
  }

  const counts = report.findings.reduce<Record<string, number>>((accumulator, finding) => {
    accumulator[finding.severity] = (accumulator[finding.severity] ?? 0) + 1;
    return accumulator;
  }, {});

  return (
    <section className="panel findings" aria-label="Structural findings">
      <h2>
        Findings
        <span className="count" aria-label={`${report.findings.length} findings`}>
          {report.findings.length}
        </span>
      </h2>
      {report.findings.length === 0 ? (
        <p>
          No structural findings in the implemented checks
          {Object.keys(counts).length > 0 ? ` (${JSON.stringify(counts)})` : ""}.
        </p>
      ) : (
        <ul className="finding-list">
          {report.findings.map((finding) => (
            <li key={finding.id} className={`finding finding--${finding.severity}`}>
              <div className="finding__head">
                <strong>{finding.severity}</strong>
                <span>{finding.proof_status}</span>
                <code>{finding.id}</code>
              </div>
              <p>{finding.message}</p>
            </li>
          ))}
        </ul>
      )}
      <p className="caveat">
        Coverage: {(report as CheckReport & { coverage?: { scope?: string } }).coverage?.scope ?? "structural"} checks
        only. An empty list means the supplied graph passed those checks; it does not prove routing,
        guard or quality correctness.
      </p>
    </section>
  );
}
