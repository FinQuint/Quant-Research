"""Planning golden objectives (Phase 9 exit test).

    python -m finquint.orchestration.evals.planning                   # compare with the baseline
    python -m finquint.orchestration.evals.planning --write-baseline  # humans only

Exit status 1 when a result differs from the baseline or an exit criterion fails.
"""
import argparse
import sys

from . import BASELINE_PATH, document, load_baseline, write_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the planning objectives.")
    parser.add_argument("--write-baseline", action="store_true", help="write baseline.json (humans only)")
    args = parser.parse_args(argv)
    if args.write_baseline:
        doc = write_baseline()
        print(f"wrote {BASELINE_PATH}; {doc['summary']}")
        return 0
    doc = document()
    baseline = load_baseline()
    problems = [f"{name}: result differs from the baseline"
                for name in sorted(set(doc["tasks"]) | set(baseline["tasks"]))
                if doc["tasks"].get(name) != baseline["tasks"].get(name)]
    problems += [f"{name}: {check} failed" for name, r in sorted(doc["tasks"].items())
                 for check, ok in sorted(r["checks"].items()) if not ok]
    print(f"{'task':32} {'kind':10} {'status':10} calls attempts passed")
    for name, r in sorted(doc["tasks"].items()):
        m = r["metrics"]
        print(f"{name:32} {r['kind']:10} {str(m['status']):10} {m['model_calls']:5} {len(m['attempt_outcomes']):8} "
              f"{r['passed']}/{r['total']}")
    print(f"summary: {doc['summary']}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
