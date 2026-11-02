"""Run the golden suite. Exit status 1 on any regression or replay divergence.

    python -m finquint.orchestration.evals [--task NAME ...] [--replay] [--json]
    python -m finquint.orchestration.evals --write-baseline --record-traces

Writing the baseline or traces changes the protected evaluator: do it only by hand, in a
reviewed PR, after checking why the scores moved.
"""
import argparse
import json
import sys

from . import BASELINE_PATH, SUITE, TRACES_DIR, Trace, compare, load_baseline, replay_task, run_task, save_baseline
from . import task as find_task
from .replay import trace_path


def main(argv=None, *, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Score the golden tasks and fault suite.")
    parser.add_argument("--task", action="append", help="run only this task (repeatable)")
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    parser.add_argument("--traces", default=str(TRACES_DIR))
    parser.add_argument("--replay", action="store_true", help="replay recorded traces instead of running live")
    parser.add_argument("--write-baseline", action="store_true", help="record current scores as the baseline")
    parser.add_argument("--record-traces", action="store_true", help="record a replay trace for every task")
    parser.add_argument("--json", action="store_true", help="print scores as JSON")
    args = parser.parse_args(argv)

    tasks = [find_task(name) for name in args.task] if args.task else list(SUITE)
    if args.replay and (args.write_baseline or args.record_traces):
        parser.error("--replay cannot be combined with recording")
    if args.task and args.write_baseline:
        parser.error("--write-baseline records the whole suite; drop --task")

    runs, divergent = {}, {}
    for item in tasks:
        if args.replay:
            run = replay_task(item, Trace.load(trace_path(item.name, args.traces)))
            if run.divergences:
                divergent[item.name] = list(run.divergences)
        else:
            run = run_task(item, mode="record" if args.record_traces else "live")
            if args.record_traces:
                run.trace.save(trace_path(item.name, args.traces))
        runs[item.name] = run
    scores = {name: run.score for name, run in runs.items()}

    if args.write_baseline:
        save_baseline(scores, args.baseline)
        comparison = None
    else:
        baseline = load_baseline(args.baseline)
        if args.task:
            baseline = {name: score for name, score in baseline.items() if name in scores}
        comparison = compare(scores, baseline)

    if args.json:
        print(json.dumps({"scores": {n: s.to_dict() for n, s in scores.items()},
                          "regressions": list(comparison.regressions) if comparison else [],
                          "improvements": list(comparison.improvements) if comparison else [],
                          "changes": list(comparison.changes) if comparison else [],
                          "divergences": divergent}, indent=2, sort_keys=True), file=out)
    else:
        for name, score in scores.items():
            gap = "  (known gap)" if score.known_gap else ""
            print(f"{score.score:6.3f}  {score.kind:<8}  {name}{gap}", file=out)
        if comparison is not None:
            for label, items in (("regression", comparison.regressions), ("improvement", comparison.improvements),
                                 ("change", comparison.changes)):
                for line in items:
                    print(f"{label}: {line}", file=out)
        for name, lines in divergent.items():
            for line in lines:
                print(f"divergence: {name}: {line}", file=out)
        if args.write_baseline:
            print(f"baseline written: {args.baseline}", file=out)
        if args.record_traces:
            print(f"traces written: {args.traces}", file=out)
    failed = bool(divergent) or (comparison is not None and not comparison.ok)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
