"""Coding golden objectives (Phase 10 exit test).

    python -m finquint.orchestration.evals.coding                   # compare with the baseline
    python -m finquint.orchestration.evals.coding --write-baseline  # humans only

Exit status 1 when a result differs from the baseline or an exit criterion fails.
"""
import argparse
import sys

from . import BASELINE_PATH, document, load_baseline, write_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the coding objectives.")
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
    print(f"{'task':38} {'kind':14} {'status':10} calls stages                     passed")
    for name, r in sorted(doc["tasks"].items()):
        m = r["metrics"]
        print(f"{name:38} {r['kind']:14} {str(m['status']):10} {m['model_calls']:5} "
              f"{','.join(m['attempt_stages']):26} {r['passed']}/{r['total']}")
    print(f"summary: {doc['summary']}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
