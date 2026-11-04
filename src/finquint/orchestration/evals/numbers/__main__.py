"""Report golden tasks (Phase 6 exit test).

    python -m finquint.orchestration.evals.numbers                    # compare with the baseline
    python -m finquint.orchestration.evals.numbers --write-baseline   # humans only

Exit status 1 when a result differs from the baseline or a reported number does not trace.
"""
import argparse
import sys

from . import BASELINE_PATH, PIPELINES, load_baseline, run_suite, totals, traced_share, write_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the report tasks (unchecked vs provenance).")
    parser.add_argument("--write-baseline", action="store_true", help="write baseline.json (humans only)")
    args = parser.parse_args(argv)
    if args.write_baseline:
        document = write_baseline()
        print(f"wrote {BASELINE_PATH}; traced share {document['traced_share']}")
        return 0
    results = run_suite()
    baseline = load_baseline()
    problems = [f"{name}: result differs from the baseline" for name in sorted(set(results) | set(baseline["tasks"]))
                if results.get(name) != baseline["tasks"].get(name)]
    if traced_share(results, "provenance") != 1.0:
        problems.append("not every reported number traces to an artifact")
    if totals(results, "provenance")["wrong_status"]:
        problems.append("a run ended differently from expected")
    print(f"{'task':26} {'pipeline':10} status    reported traced invented wrong")
    for name, pair in sorted(results.items()):
        for pipeline in PIPELINES:
            m = pair[pipeline]
            print(f"{name:26} {pipeline:10} {m['status']:9} {m['numbers_reported']:8} {m['numbers_traced']:6} "
                  f"{m['invented_reported']:8} {m['wrong_status']:5}")
    for pipeline in PIPELINES:
        print(f"total {pipeline}: {totals(results, pipeline)}; traced share {traced_share(results, pipeline)}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
