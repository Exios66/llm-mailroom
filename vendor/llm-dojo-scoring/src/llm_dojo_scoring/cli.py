"""Command-line interface.

- ``dojo-analyze`` — analyze a results workbook / JSONL log / Langfuse project
  into a Markdown report + PNG plots.
- ``dojo-export`` — regenerate the canonical experiment-log workbooks +
  codebooks from a JSONL log.
- ``dojo-sync`` — pull live experiment traces from Langfuse (and report the
  local Phoenix sink status) into the reference workbook + analysis frame.
"""

from __future__ import annotations

import argparse
import os
import sys


def _cli_sync(argv: list[str]) -> int:
    """Parse sync options, optionally probe Phoenix, and export Langfuse results."""
    parser = argparse.ArgumentParser(
        prog="dojo-sync",
        description="Sync the dojo scoring pipeline with the live trace sinks "
                    "(Langfuse project + local Phoenix/OTLP).",
    )
    parser.add_argument("--task", default="subtype_classification",
                        help="Langfuse trace name to sync (default: "
                             "subtype_classification)")
    parser.add_argument("--session", default=None,
                        help="Restrict to one session/experiment name")
    parser.add_argument("--max-items", type=int, default=None,
                        help="Cap on fetched traces (None = all)")
    parser.add_argument("--env-file", default=None,
                        help="Credential file (langfuse.env style)")
    parser.add_argument("--outdir", default=".",
                        help="Output directory for the workbook + codebook")
    parser.add_argument("--workbook", default=None,
                        help="Workbook filename (default: Sorter_Experiment_Results.xlsx)")
    parser.add_argument("--no-workbook", action="store_true",
                        help="Skip writing the workbook; print the frame summary")
    parser.add_argument("--check-phoenix", action="store_true",
                        help="Probe the local Phoenix/OTLP sink and report status")
    args = parser.parse_args(argv)

    from .langfuse_sync import LangfuseClient, load_langfuse_config, sync_sorter_results

    # --check-phoenix alone is a standalone probe (no Langfuse credentials needed)
    sync_requested = any([args.session, args.max_items, args.env_file, args.workbook,
                          args.task != "subtype_classification",
                          args.outdir != ".", args.no_workbook])
    if args.check_phoenix:
        from .phoenix_sync import PhoenixClient, check_phoenix

        status = check_phoenix()
        print(f"[dojo-sync] {status.describe()}")
        if status.available:
            client = PhoenixClient()
            spans = client.spans()
            if spans is None:
                print(f"[dojo-sync] span read failed: {client.error or 'unknown'}")
            else:
                print(f"[dojo-sync] {len(spans)} spans in project 'default'.")
        if not sync_requested:
            return 0

    try:
        client = LangfuseClient(load_langfuse_config(args.env_file))
    except ValueError as exc:
        print(f"[dojo-sync] {exc}", file=sys.stderr)
        return 1
    print(f"[dojo-sync] Langfuse: {client.config.base_url} project={client.config.project}")

    workbook = False if args.no_workbook else (args.workbook or "Sorter_Experiment_Results.xlsx")
    frame, path = sync_sorter_results(
        outdir=args.outdir, task=args.task, max_items=args.max_items,
        env_file=args.env_file, workbook=workbook, session=args.session,
    )
    print(f"[dojo-sync] {len(frame)} runs synced from trace '{args.task}'")
    if path:
        print(f"[dojo-sync] workbook -> {path}")
    cols = [c for c in ("DATE", "Experiment Name", "SAMPLE (n)", "MODEL",
                        "Prompt Version", "Subtype Accuracy", "Subtype Accuracy (equiv)")
            if c in frame.columns]
    if not frame.empty and cols:
        print(frame[cols].to_string(index=False, max_rows=20))
    return 0


def _cli_analyze(argv: list[str]) -> int:
    """Load evaluation results and write a report with optional diagnostic plots."""
    parser = argparse.ArgumentParser(
        prog="dojo-analyze",
        description="Analyze an evaluation results workbook (or JSONL log): "
                    "scoring summary, error analysis, interpretation, plots.",
    )
    parser.add_argument("input", help="Path to a results workbook (.xlsx) or experiment-log JSONL")
    parser.add_argument("-o", "--out", default=None,
                        help="Markdown report path (default: <input>.report.md)")
    parser.add_argument("--plots", default=None,
                        help="Directory for PNG plots (default: <input>_plots/)")
    parser.add_argument("--no-plots", action="store_true", help="Skip plot generation")
    parser.add_argument("--metric", default="Subtype Accuracy",
                        help="Primary metric column (default: Subtype Accuracy)")
    parser.add_argument("--target", type=float, default=None,
                        help="Accuracy target (0-1) for the target-gate verdict")
    parser.add_argument("--min-n", type=int, default=0,
                        help="Minimum sample size for the champion run")
    parser.add_argument("--cost-column", default=None,
                        help="Cost column for the efficiency plot/note (default: Cost Estimated USD)")
    parser.add_argument("--task", default=None,
                        help="Task filter when the input is a JSONL log "
                             "(e.g. subtype_classification)")
    parser.add_argument("--max-items", type=int, default=None,
                        help="Cap on traces fetched when the input is a "
                             "langfuse:<name> live sync")
    args = parser.parse_args(argv)

    from . import error_analysis as ea
    from .io import load_log, normalize_results_frame, read_workbook
    from .report import build_report

    path = args.input
    if path.startswith("langfuse:"):
        # Live sync: langfuse:<trace-name>  (e.g. langfuse:subtype_classification)
        from .langfuse_sync import (
            LangfuseClient,
            fetch_run_records,
            load_langfuse_config,
            records_to_sorter_frame,
        )

        trace_name = path.split(":", 1)[1] or "subtype_classification"
        client = LangfuseClient(load_langfuse_config())
        records = fetch_run_records(client, task=trace_name, max_items=args.max_items)
        frame = records_to_sorter_frame(records)
        result = type("SyncResult", (), {
            "kind": "langfuse", "n_runs": len(frame), "frame": frame,
            "path": args.input,
        })()
        print(f"[dojo-analyze] synced {len(records)} runs from Langfuse trace '{trace_name}'",
              file=sys.stderr)
    elif path.endswith(".jsonl"):
        result = load_log(path, task=args.task)
    else:
        result = read_workbook(path)
    frame = normalize_results_frame(result.frame)
    if frame.empty:
        print(f"[dojo-analyze] no rows in {path}", file=sys.stderr)
        return 1

    cost_column = args.cost_column or ("Cost Estimated USD" if "Cost Estimated USD" in frame.columns else None)
    metric = args.metric if args.metric in frame.columns else ea.DEFAULT_METRIC
    if metric != args.metric:
        print(f"[dojo-analyze] metric '{args.metric}' not found — using '{metric}'", file=sys.stderr)

    out = args.out or (path + ".report.md")
    plots_dir = args.plots or (path + "_plots")

    plot_paths: dict[str, str] | None = None
    if not args.no_plots:
        try:
            import matplotlib

            matplotlib.use("Agg")
            from .visualize import build_all_plots, save_plots

            figures = build_all_plots(frame, metric=metric, cost_column=cost_column)
            saved = save_plots(figures, plots_dir, prefix="dojo")
            plot_paths = {os.path.basename(p).removeprefix("dojo_").removesuffix(".png"): p
                          for p in saved}
        except Exception as exc:  # pragma: no cover
            print(f"[dojo-analyze] plot generation failed: {exc}", file=sys.stderr)

    report = build_report(
        frame, path=out, metric=metric, target=args.target, min_n=args.min_n,
        cost_column=cost_column, plot_paths=plot_paths,
        title=f"{result.kind.capitalize()} Evaluation Report — {os.path.basename(path)}",
    )
    print(f"[dojo-analyze] {result.n_runs} runs -> {out}")
    if plot_paths:
        for p in plot_paths.values():
            print(f"[dojo-analyze]   plot: {p}")
    return 0


def _cli_export(argv: list[str]) -> int:
    """Parse export options and regenerate experiment workbooks and codebooks."""
    parser = argparse.ArgumentParser(
        prog="dojo-export",
        description="Regenerate per-task experiment performance workbooks + "
                    "codebooks (Google-Sheets-friendly, reference formats).",
    )
    parser.add_argument("--task", choices=["sorter", "extraction", "all"], default="all",
                        help="Which task workbook(s) to regenerate (default: all)")
    parser.add_argument("--outdir", default=".",
                        help="Output directory for the workbooks + codebooks")
    parser.add_argument("--log", default="reports/experiment_log.jsonl",
                        help="Path to the experiment log")
    parser.add_argument("--sweep", action="store_true",
                        help="Also write the model-sweep workbook (champion prompt)")
    args = parser.parse_args(argv)

    from .export import (
        extraction_columns, extraction_records, load_records, sorter_columns,
        sorter_records, build_sweep_workbook, write_codebook, write_workbook,
    )

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    records = load_records(args.log)
    tasks = ["sorter", "extraction"] if args.task == "all" else [args.task]
    for task in tasks:
        if task == "sorter":
            cols, recs = sorter_columns(), sorter_records(records)
            wb_path = os.path.join(outdir, "Sorter_Experiment_Results.xlsx")
            cb_path = os.path.join(outdir, "Sorter_Experiment_Codebook.csv")
            with_codebook = True
        else:
            cols, recs = extraction_columns(), extraction_records(records)
            wb_path = os.path.join(outdir, "Entity_Extraction_Results.xlsx")
            cb_path = os.path.join(outdir, "Entity_Extraction_Codebook.csv")
            with_codebook = False
        write_workbook(wb_path, "Eval Results", cols, recs, codebook_sheet=with_codebook)
        write_codebook(cb_path, cols)
        print(f"[dojo-export] {task}: {len(recs)} runs -> {wb_path} ({len(cols)} cols) + {cb_path}")
    if args.sweep and "sorter" in tasks:
        sweep_path, n = build_sweep_workbook(records, outdir=outdir)
        print(f"[dojo-export] sweep: {n} runs -> {sweep_path}")
    return 0


def analyze_main(argv: list[str] | None = None) -> None:
    """Run the analysis CLI and exit with its status code."""
    raise SystemExit(_cli_analyze(sys.argv[1:] if argv is None else argv))


def export_main(argv: list[str] | None = None) -> None:
    """Run the export CLI and exit with its status code."""
    raise SystemExit(_cli_export(sys.argv[1:] if argv is None else argv))


def sync_main(argv: list[str] | None = None) -> None:
    """Run the synchronization CLI and exit with its status code."""
    raise SystemExit(_cli_sync(sys.argv[1:] if argv is None else argv))


__all__ = ["_cli_analyze", "_cli_export", "_cli_sync",
           "analyze_main", "export_main", "sync_main"]

_COMMANDS = {
    "analyze": analyze_main,
    "export": export_main,
    "sync": sync_main,
}


def _dispatch(argv: list[str]) -> int:
    """Route ``python -m llm_dojo_scoring.cli <command> [args...]``.

    A leading subcommand selects the entry point (``dojo-analyze`` /
    ``dojo-export`` / ``dojo-sync``); anything else is treated as a direct
    ``dojo-analyze`` invocation (a bare input path / ``-o`` report), which is
    the module's primary use. Console-script entry points call the mains with
    no argv and read ``sys.argv[1:]`` themselves.
    """
    if argv and argv[0] in _COMMANDS:
        return _COMMANDS[argv[0]](argv[1:])
    return analyze_main(argv)


if __name__ == "__main__":
    raise SystemExit(_dispatch(sys.argv[1:]))
