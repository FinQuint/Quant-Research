"""Full-stack soak (Phase 7 exit test).

    python -m finquint.orchestration.evals.soak                    # compare with the baseline
    python -m finquint.orchestration.evals.soak --write-baseline   # humans only

Exit status 1 when a criterion fails or the score differs from the baseline.
"""
import argparse
import json
import sys

from . import BASELINE_PATH, baseline_document, load_baseline, run_soak, write_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the full-stack soak campaign and score it.")
    parser.add_argument("--write-baseline", action="store_true", help="write baseline.json (humans only)")
    args = parser.parse_args(argv)
    if args.write_baseline:
        document = write_baseline()
        print(f"wrote {BASELINE_PATH}; {document['grade']['passed']}/{document['grade']['total']} criteria")
        return 0
    document = baseline_document(run_soak())
    problems = [f"criterion failed: {name}" for name, ok in document["grade"]["criteria"].items() if not ok]
    baseline = load_baseline()
    for section in ("metrics", "facts", "grade", "policy"):
        if document[section] != baseline.get(section):
            problems.append(f"{section} differ from the baseline")
    print(json.dumps({"metrics": document["metrics"], "grade": f"{document['grade']['passed']}/"
                      f"{document['grade']['total']}"}, indent=1))
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
