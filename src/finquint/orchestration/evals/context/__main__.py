"""Long-run context evals (Phase 5 exit test).

    python -m finquint.orchestration.evals.context                    # score against the baseline
    python -m finquint.orchestration.evals.context --sweep            # also re-run the threshold sweeps
    python -m finquint.orchestration.evals.context --write-baseline   # humans only: baseline + config

Exit status 1 when a result differs from the recorded baseline or the config does not hold the
values the sweeps picked.
"""
import argparse
import json
import sys

from ...context.builder import ContextPolicy
from . import BASELINE_PATH, SPEC_DEFAULTS, load_baseline, pick_policy, run_suite, totals, write_baseline


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the long-run context tasks (naive vs builder).")
    parser.add_argument("--sweep", action="store_true", help="re-run the sweeps and check the picked values")
    parser.add_argument("--write-baseline", action="store_true",
                        help="re-run sweeps, write baseline.json and configs/context_policy.json (humans only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.write_baseline:
        document = write_baseline()
        print(f"wrote {BASELINE_PATH} and configs/context_policy.json; picked {document['picked']}")
        return 0

    policy = ContextPolicy.load()
    baseline = load_baseline()
    results = run_suite(policy)
    problems = []
    for name in sorted(set(results) | set(baseline["tasks"])):
        if results.get(name) != baseline["tasks"].get(name):
            problems.append(f"{name}: result differs from the baseline")
    config = {k: getattr(policy, k) for k in SPEC_DEFAULTS}
    if config != baseline["picked"]:
        problems.append(f"configs/context_policy.json {config} does not match the sweep's picks {baseline['picked']}")
    if args.sweep:
        picked, _ = pick_policy()
        picked = {k: getattr(picked, k) for k in SPEC_DEFAULTS}
        if picked != baseline["picked"]:
            problems.append(f"the sweep now picks {picked}, the baseline recorded {baseline['picked']}")

    if args.json:
        print(json.dumps({"tasks": results, "problems": problems}, indent=1, sort_keys=True))
    else:
        print(f"{'task':32} {'strategy':8} forgotten citations wrong_status calls rebuilds tokens")
        for name, pair in sorted(results.items()):
            for strategy in ("naive", "builder"):
                m = pair[strategy]
                print(f"{name:32} {strategy:8} {m['forgotten_criteria']:9} {m['citation_failures']:9} "
                      f"{m['wrong_status']:12} {m['model_calls']:5} {m['rebuilds']:8} {m['input_tokens']:6}")
        for strategy in ("naive", "builder"):
            print(f"total {strategy}: {totals(results, strategy)}")
        for problem in problems:
            print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
