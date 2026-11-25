"""Template conformance suite (ADR-023, protected). Runs inside the sandbox, never in the host.

    python -I conformance.py <request.json>

request.json (written by the host): {"src": host source root, "module_file": the candidate module,
"module": its name, "bars": CSV of the pinned dataset's train window, "symbol": its symbol,
"max_backtest_seconds": ..., "lookahead_cutoffs": ...}. The result is printed as one line starting
with RESULT_MARKER followed by JSON: {"passed", "problems", "tests": {name: {"passed", "evidence"}}}.
The static check bans print, so a candidate cannot forge that line.

Tests, in order (a failure in import stops the rest):
  import        the module imports and exports a StrategyTemplate named TEMPLATE
  build         build() succeeds at the defaults and at each parameter's bounds (booleans: both)
  bounds_run    a backtest runs at each of those parameter sets
  determinism   two fresh builds on the same data give identical orders
  lookahead     orders up to each cutoff are unchanged when later bars are removed (check_lookahead)
  orders        orders name the dataset's symbol, have finite nonzero quantities and bar timestamps
  trades        at least one order over the window at the defaults
  runtime       the default backtest takes at most max_backtest_seconds
"""
import json
import sys
import time

RESULT_MARKER = "FINQUINT_CONFORMANCE_RESULT "


def _parameter_sets(tpl):
    defaults = {name: spec.default for name, spec in tpl.params.items()}
    sets = [("defaults", dict(defaults))]
    for name, spec in sorted(tpl.params.items()):
        values = (False, True) if spec.kind == "boolean" else (spec.minimum, spec.maximum)
        for value in values:
            if value != defaults[name]:
                sets.append((f"{name}={value}", {**defaults, name: value}))
    return sets


def run(request):
    sys.path.insert(0, request["src"])
    import importlib.util
    from math import isfinite

    import pandas as pd

    from finquint.backtesting import BacktestConfig, EventDrivenBacktester, validate_bars
    from finquint.orchestration.verify.invariants import _orders, _same_orders, check_lookahead
    from finquint.strategies.base import StrategyTemplate

    tests = {}

    def record(name, passed, evidence):
        tests[name] = {"passed": bool(passed), "evidence": str(evidence)[:500]}
        return passed

    try:
        spec = importlib.util.spec_from_file_location(f"finquint_candidate.{request['module']}", request["module_file"])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tpl = getattr(module, "TEMPLATE", None)
        if not isinstance(tpl, StrategyTemplate):
            raise TypeError("TEMPLATE is not a StrategyTemplate")
        record("import", True, f"imported; template {tpl.name}")
    except Exception as exc:
        record("import", False, f"{type(exc).__name__}: {exc}")
        return tests

    bars = validate_bars(pd.read_csv(request["bars"]))
    symbol = request["symbol"]
    engine = EventDrivenBacktester(BacktestConfig())
    sets = _parameter_sets(tpl)

    built, build_errors = [], []
    for label, values in sets:
        try:
            tpl.build(symbol, values)
            built.append((label, values))
        except Exception as exc:
            build_errors.append(f"{label}: {type(exc).__name__}: {exc}")
    record("build", not build_errors, "; ".join(build_errors) or f"built at {len(sets)} parameter sets")

    run_errors, default_result, seconds = [], None, None
    for label, values in built:
        try:
            start = time.perf_counter()
            result = engine.run(bars, tpl.build(symbol, values))
            if label == "defaults":
                default_result, seconds = result, time.perf_counter() - start
        except Exception as exc:
            run_errors.append(f"{label}: {type(exc).__name__}: {exc}")
    record("bounds_run", not run_errors and default_result is not None,
           "; ".join(run_errors) or f"backtests ran at {len(built)} parameter sets")
    if default_result is None:
        return tests

    try:
        again = engine.run(bars, tpl.build(symbol, dict(sets[0][1])))
        same = _same_orders(_orders(default_result), _orders(again))
        record("determinism", same, "two fresh runs gave identical orders" if same
               else "two fresh runs on the same data gave different orders")
    except Exception as exc:
        record("determinism", False, f"{type(exc).__name__}: {exc}")

    look = check_lookahead(bars, lambda frame: tpl.build(symbol, dict(sets[0][1])), backtester=engine,
                           cutoff_count=request["lookahead_cutoffs"])
    record("lookahead", look.passed, look.evidence)

    orders = default_result.orders
    stamps = set(pd.to_datetime(bars["timestamp"], utc=True))
    bad = []
    for row in orders.itertuples(index=False):
        quantity = float(row.quantity)
        if str(row.symbol) != symbol:
            bad.append(f"order {row.order_id} names symbol {row.symbol}")
        if not isfinite(quantity) or quantity == 0:
            bad.append(f"order {row.order_id} has quantity {quantity}")
        if pd.Timestamp(row.submitted_at) not in stamps:
            bad.append(f"order {row.order_id} is stamped {row.submitted_at}, not a bar time")
    record("orders", not bad, "; ".join(bad[:5]) or f"{len(orders)} well-formed orders")
    record("trades", len(orders) > 0, f"{len(orders)} orders at the defaults")
    limit = request["max_backtest_seconds"]
    record("runtime", seconds <= limit, f"default backtest took {seconds:.2f} s (limit {limit} s)")
    return tests


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    with open(argv[0], encoding="utf-8") as handle:
        request = json.load(handle)
    try:
        tests = run(request)
    except Exception as exc:          # the suite itself failed: report, never pass
        tests = {"suite": {"passed": False, "evidence": f"{type(exc).__name__}: {exc}"[:500]}}
    problems = [f"conformance: {name}: {t['evidence']}" for name, t in tests.items() if not t["passed"]]
    result = {"passed": not problems and "import" in tests, "problems": problems, "tests": tests}
    sys.stdout.write(RESULT_MARKER + json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
