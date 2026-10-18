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
