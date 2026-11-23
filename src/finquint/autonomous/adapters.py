"""Reference adapters; existing numerical and pipeline APIs stay unchanged."""
from math import isclose

from finquint.agents import ValidationResult
from finquint.fixed_income import Bond, price_bond, yield_to_maturity
from finquint.fixed_income.stages import BondPricingStage, BondRiskStage
from finquint.pipeline import QuantPipeline

from .registry import Method, MethodRegistry


def pipeline_adapter(factory, *, load_data=lambda parameters: None, encode=lambda result: result.context.results):
    """Build a fresh pipeline/data per call; explicitly encode non-JSON outputs.

    The host supplies factories, never model-provided import paths or code.
    A pipeline configured to continue after errors is still rejected on any error.
    """
    def execute(parameters, context):
        pipeline = factory(parameters)
        if not isinstance(pipeline, QuantPipeline):
            raise TypeError("factory must produce a QuantPipeline")
        result = pipeline.run(load_data(parameters))
        if result.context.errors:
            raise ValueError(f"pipeline contains failed stages: {result.context.errors}")
        return encode(result)
    return execute


def evaluation_gate(name, suite, data_factory, *, metadata_factory=lambda p, c, o: {}):
    """Adapt Phase 10C checks to mandatory supervisor validation."""
    def gate(parameters, context, output):
        report = suite.evaluate(data_factory(parameters, context, output),
                                metadata=metadata_factory(parameters, context, output))
        return ValidationResult(name, report.passed, str(report.to_dict()),
                                {"failures": len(report.failures), "warnings": len(report.warnings)})
    return gate


def _bond_input(parameters):
    if set(parameters) != {"bond", "annual_yield"}:
        raise ValueError("provide exactly bond and annual_yield")
    bond = Bond(**parameters["bond"])
    price_bond(bond, parameters["annual_yield"])


def _bond_pipeline(parameters):
    bond = Bond(**parameters["bond"])
    return (QuantPipeline("autonomous_bond")
            .add(BondPricingStage(bond, annual_yield=parameters["annual_yield"]))
            .add(BondRiskStage(bond)))


def _bond_gate(parameters, context, output):
    bond = Bond(**parameters["bond"])
    ytm = parameters["annual_yield"]
    price = output["bond_price"]
    recovered = yield_to_maturity(bond, price)
    bump = 1e-5
    derivative = (price_bond(bond, ytm - bump) - price_bond(bond, ytm + bump)) / (2 * bump)
    passed = (isclose(recovered, ytm, abs_tol=1e-8)
              and isclose(output["modified_duration"] * price, derivative, rel_tol=1e-6)
              and output["convexity"] >= 0 and output["dv01"] >= 0)
    return ValidationResult("bond_roundtrip_and_duration", passed,
                            "YTM round trip and independent finite-difference duration checked.")


def _source_input(parameters):
    if set(parameters) != {"source_ids"} or not isinstance(parameters["source_ids"], list) or not parameters["source_ids"]:
        raise ValueError("provide a nonempty source_ids list")
    if not all(isinstance(s, str) for s in parameters["source_ids"]):
        raise ValueError("source IDs must be strings")


def _read_sources(parameters, context):
    sources = {s["source_id"]: s for s in context["sources"]}
    return {"findings": [{"source_id": key, "quote": sources[key]["content"]}
                         for key in parameters["source_ids"]]}


def _source_gate(parameters, context, output):
    ids = [f["source_id"] for f in output["findings"]]
    return ValidationResult("requested_source_coverage", ids == parameters["source_ids"],
                            "Every requested source appears in the extractive research result.")


def default_registry():
    return (MethodRegistry()
            .register(Method("source_review", "1", "research", _read_sources, _source_input, (_source_gate,)))
            .register(Method("bond_analytics", "1", "execution", pipeline_adapter(_bond_pipeline),
                             _bond_input, (_bond_gate,))))


# --- risk methods (Phase 8): wrappers around finquint.risk.historical, numerics unchanged ---------

def _number_list(values, name):
    if not isinstance(values, list) or not values or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        raise ValueError(f"{name} must be a nonempty list of numbers")


def _rolling_vol_input(parameters):
    if set(parameters) != {"returns", "window", "annualization"}:
        raise ValueError("provide exactly returns, window and annualization")
    _number_list(parameters["returns"], "returns")
    window = parameters["window"]
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError("window must be an integer of at least two")


def _rolling_vol(parameters, context):
    from finquint.risk.historical import rolling_volatility
    series = rolling_volatility(parameters["returns"], parameters["window"],
                                annualization=parameters["annualization"])
    complete = [v for v in series if v is not None]
    return {"volatility": complete[-1] if complete else None, "complete_windows": len(complete)}


def _rolling_vol_gate(parameters, context, output):
    from math import sqrt
    from statistics import stdev
    returns, window = parameters["returns"], parameters["window"]
    expected = len(returns) - window + 1 if window <= len(returns) else 0
    if expected < 1:
        return ValidationResult("rolling_volatility_window", False,
                                f"no complete window: {len(returns)} returns, window {window}")
    check = stdev(returns[-window:]) * sqrt(parameters["annualization"])
    passed = output["complete_windows"] == expected and isclose(output["volatility"], check, rel_tol=1e-12)
    return ValidationResult("rolling_volatility_window", passed,
                            f"{expected} complete windows; last window recomputed independently")


def _var_input(parameters):
    if set(parameters) != {"pnl", "confidence"}:
        raise ValueError("provide exactly pnl and confidence")
    _number_list(parameters["pnl"], "pnl")
    confidence = parameters["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 < confidence < 1:
        raise ValueError("confidence must lie strictly between zero and one")


def _historical_var(parameters, context):
    from finquint.risk.historical import historical_var
    return {"var": historical_var(parameters["pnl"], parameters["confidence"]), "model": "historical"}


def _parametric_var(parameters, context):
    from finquint.risk.historical import parametric_var
    return {"var": parametric_var(parameters["pnl"], parameters["confidence"]), "model": "parametric"}


HISTORICAL_VAR_MIN_TAIL = 1      # at least one expected tail observation: n >= 1 / (1 - confidence)
PARAMETRIC_VAR_MIN_OBS = 30


def _historical_var_gate(parameters, context, output):
    from math import ceil
    n, confidence = len(parameters["pnl"]), parameters["confidence"]
    need = ceil(HISTORICAL_VAR_MIN_TAIL / (1 - confidence) - 1e-9)
    if n < need:
        return ValidationResult("historical_var_sample", False,
                                f"historical VaR at confidence {confidence} needs at least {need} observations; got {n}")
    losses = sorted(-v for v in parameters["pnl"])
    position = confidence * (n - 1)
    lower, weight = int(position), position - int(position)
    quantile = losses[lower] if lower == n - 1 else losses[lower] * (1 - weight) + losses[lower + 1] * weight
    return ValidationResult("historical_var_sample", isclose(output["var"], max(0.0, quantile), abs_tol=1e-12),
                            f"{n} observations; empirical quantile recomputed independently")


def _parametric_var_gate(parameters, context, output):
    from statistics import NormalDist, fmean, stdev
    n, pnl = len(parameters["pnl"]), parameters["pnl"]
    if n < PARAMETRIC_VAR_MIN_OBS:
        return ValidationResult("parametric_var_sample", False,
                                f"parametric VaR needs at least {PARAMETRIC_VAR_MIN_OBS} observations; got {n}")
    check = max(0.0, -fmean(pnl) + NormalDist().inv_cdf(parameters["confidence"]) * stdev(pnl))
    return ValidationResult("parametric_var_sample", isclose(output["var"], check, rel_tol=1e-12),
                            f"{n} observations; normal quantile recomputed independently")


def risk_methods():
    """Risk methods with obvious repair levers (Phase 8): a rolling window, and two VaR models
    that answer the same question with different sample needs."""
    return (Method("rolling_volatility", "1", "execution", _rolling_vol, _rolling_vol_input, (_rolling_vol_gate,)),
            Method("historical_var", "1", "execution", _historical_var, _var_input, (_historical_var_gate,)),
            Method("parametric_var", "1", "execution", _parametric_var, _var_input, (_parametric_var_gate,)))


def risk_registry():
    """default_registry() plus the risk methods. default_registry() itself is unchanged."""
    registry = default_registry()
    for method in risk_methods():
        registry.register(method)
    return registry
