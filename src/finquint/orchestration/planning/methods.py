"""Workflow methods the planner may use on pinned datasets (Phase 9). Numerics are the existing
backtester and performance metrics, unchanged; data comes from the DatasetCatalog, never inline.

  parameter_search   execution  grid over a template's bounded parameters, train window only
  backtest           execution  a template on the train or test window; params given or taken from
                                a parameter_search it depends on (params_from)
  performance_summary execution summarize_performance on a backtest's returns (its dependency)
"""
from itertools import product
from math import isclose, isfinite

from finquint.agents import ValidationResult
from finquint.autonomous import Method
from finquint.autonomous.adapters import risk_registry
from finquint.backtesting import BacktestConfig, EventDrivenBacktester
from finquint.performance.metrics import summarize_performance
from finquint import strategies

from ..verify.invariants import check_lookahead
from .catalog import WINDOWS

MAX_GRID = 50              # (tune) combinations a parameter_search may run
LOOKAHEAD_CUTOFFS = 3      # (tune) truncation points checked per backtest
OBJECTIVES = ("sharpe_ratio", "total_return")


def _config(parameters):
    return BacktestConfig(proportional_cost_bps=parameters.get("cost_bps", 0.0))


def _returns(result):
    equity = result.equity_curve["equity"].astype(float)
    returns = equity.pct_change().dropna()
    return [float(r) for r in returns]


def lookup_for(templates=None):
    """template(name) over `templates` (a name -> StrategyTemplate mapping), or the package registry."""
    if templates is None:
        return strategies.template

    def template(name):
        if name not in templates:
            raise ValueError(f"strategy template is not registered: {name}")
        return templates[name]
    return template


def _run(catalog, ref, window, strategy_name, params, cost, template=strategies.template):
    data = catalog.load(ref, window)
    entry = catalog.get(ref)
    tpl = template(strategy_name)
    result = EventDrivenBacktester(cost).run(data, tpl.build(entry.symbols[0], params))
    return data, result


def _score(returns, objective):
    if len(returns) < 2:
        return None
    summary = summarize_performance(returns)
    value = getattr(summary, objective)
    return None if value is None or not isfinite(value) else float(value)


def _common_input(parameters, catalog, *, keys, template=strategies.template):
    extra = set(parameters) - keys
    if extra:
        raise ValueError(f"unknown parameters: {sorted(extra)}")
    catalog.get(parameters.get("dataset"))
    template(parameters.get("strategy"))
    if parameters.get("window") not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    cost = parameters.get("cost_bps", 0.0)
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost <= 50:
        raise ValueError("cost_bps must be a number in [0, 50]")


def make_methods(catalog, templates=None):
    template = lookup_for(templates)

    def search_input(p):
        _common_input(p, catalog, keys={"dataset", "strategy", "window", "grid", "objective", "cost_bps"},
                      template=template)
        if p["window"] != "train":
            raise ValueError("parameter_search may only read the train window")
        if p.get("objective", "sharpe_ratio") not in OBJECTIVES:
            raise ValueError(f"objective must be one of {OBJECTIVES}")
        grid = p.get("grid")
        if not isinstance(grid, dict) or not grid or not all(isinstance(v, list) and v for v in grid.values()):
            raise ValueError("grid maps parameter names to nonempty value lists")
        tpl = template(p["strategy"])
        combos = 1
        for values in grid.values():
            combos *= len(values)
        if combos > MAX_GRID:
            raise ValueError(f"grid has {combos} combinations; at most {MAX_GRID}")
        for combo in product(*grid.values()):
            tpl.resolve(dict(zip(grid, combo)))

    def search(p, context):
        grid, objective = p["grid"], p.get("objective", "sharpe_ratio")
        scores = []
        for combo in product(*grid.values()):
            params = template(p["strategy"]).resolve(dict(zip(grid, combo)))
            _, result = _run(catalog, p["dataset"], "train", p["strategy"], params, _config(p), template)
            scores.append({"params": params, "score": _score(_returns(result), objective)})
        valid = [s for s in scores if s["score"] is not None]
        if not valid:
            raise ValueError("no parameter combination produced a score")
        best = max(valid, key=lambda s: s["score"])   # first wins on ties: grid order is deterministic
        return {"window": "train", "objective": objective, "best_params": best["params"],
                "best_score": best["score"], "scores": scores}

    def search_gate(p, context, output):
        valid = [s for s in output["scores"] if s["score"] is not None]
        best = max(valid, key=lambda s: s["score"]) if valid else None
        passed = (output["window"] == "train" and best is not None and best["params"] == output["best_params"]
                  and len(output["scores"]) == _grid_size(p["grid"]))
        return ValidationResult("search_train_only_and_argmax", passed,
                                f"{len(output['scores'])} combinations on the train window; best re-derived")

    def backtest_input(p):
        _common_input(p, catalog, keys={"dataset", "strategy", "window", "params", "params_from", "cost_bps"},
                      template=template)
        if ("params" in p) == ("params_from" in p):
            raise ValueError("give exactly one of params or params_from")
        if "params" in p:
            template(p["strategy"]).resolve(p["params"])
        elif not isinstance(p["params_from"], str) or not p["params_from"].strip():
            raise ValueError("params_from must name the parameter_search task this backtest depends on")

    def backtest_params(p, context):
        if "params" in p:
            return template(p["strategy"]).resolve(p["params"])
        source = context["dependencies"].get(p["params_from"])
        if not source or "best_params" not in source:
            raise ValueError(f"params_from {p['params_from']!r} is not a parameter_search dependency")
        return template(p["strategy"]).resolve(source["best_params"])

    def backtest(p, context):
        params = backtest_params(p, context)
        data, result = _run(catalog, p["dataset"], p["window"], p["strategy"], params, _config(p), template)
        returns = _returns(result)
        return {"window": p["window"], "strategy": p["strategy"], "params": params, "bars": len(data),
                "returns": returns, "fills": len(result.fills),
                "total_return": float(result.equity_curve["equity"].iloc[-1] / result.equity_curve["equity"].iloc[0] - 1)}

    def lookahead_gate(p, context, output):
        data = catalog.load(p["dataset"], p["window"])
        symbol = catalog.get(p["dataset"]).symbols[0]
        tpl = template(p["strategy"])
        result = check_lookahead(data, lambda frame: tpl.build(symbol, output["params"]),
                                 backtester=EventDrivenBacktester(_config(p)), cutoff_count=LOOKAHEAD_CUTOFFS)
        return ValidationResult("backtest_no_lookahead", result.passed, result.evidence)

    def window_gate(p, context, output):
        bars = len(catalog.load(p["dataset"], p["window"]))
        passed = output["bars"] == bars and len(output["returns"]) == bars - 1 and output["window"] == p["window"]
        return ValidationResult("backtest_window", passed, f"{bars} bars in the {p['window']} window")

    def perf_input(p):
        if set(p) - {"source_task", "periods_per_year"} or not isinstance(p.get("source_task"), str):
            raise ValueError("provide source_task (a backtest this call depends on) and optional periods_per_year")
        ppy = p.get("periods_per_year", 252)
        if isinstance(ppy, bool) or not isinstance(ppy, int) or not 1 <= ppy <= 366:
            raise ValueError("periods_per_year must be an integer in [1, 366]")

    def perf(p, context):
        source = context["dependencies"].get(p["source_task"])
        if not source or "returns" not in source:
            raise ValueError(f"source_task {p['source_task']!r} is not a backtest dependency")
        summary = summarize_performance(source["returns"], periods_per_year=p.get("periods_per_year", 252)).to_dict()
        return {"window": source["window"], "source_task": p["source_task"],
                **{k: (None if v is None else float(v)) for k, v in summary.items()}}

    def perf_gate(p, context, output):
        returns = context["dependencies"][p["source_task"]]["returns"]
        total = 1.0
        for r in returns:
            total *= 1 + r
        passed = output["observations"] == len(returns) and isclose(output["total_return"], total - 1, rel_tol=1e-9,
                                                                     abs_tol=1e-12)
        return ValidationResult("performance_recomputed", passed, "observations and total return recomputed")

    return (Method("parameter_search", "1", "execution", search, search_input, (search_gate,)),
            Method("backtest", "1", "execution", backtest, backtest_input, (window_gate, lookahead_gate)),
            Method("performance_summary", "1", "execution", perf, perf_input, (perf_gate,)))


def _grid_size(grid):
    size = 1
    for values in grid.values():
        size *= len(values)
    return size


def planning_registry(catalog, templates=None):
    """risk_registry() plus the planner methods bound to `catalog`. templates: name -> StrategyTemplate
    (default: the package registry, finquint.strategies.TEMPLATES)."""
    registry = risk_registry()
    for method in make_methods(catalog, templates):
        registry.register(method)
    return registry
