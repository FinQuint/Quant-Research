"""Offline research + deterministic analytics, with persistent resumable state."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from finquint.autonomous import AutonomousWorkflow, Call, Request, RunStore, Source
from finquint.autonomous.adapters import default_registry
from finquint.research import ResearchStore


def main():
    request = Request("bond-study", "Price and assess a synthetic fixed-rate bond", "synthetic-example-v1", (
        Call("conventions", "source_review", {"source_ids": ["methodology"]}),
        Call("calculate", "bond_analytics", {
            "bond": {"face_value": 100, "coupon_rate": .05, "maturity_years": 5, "frequency": 2},
            "annual_yield": .04}),
    ), (Source("methodology", "repo://README.md", "synthetic-example-v1",
               "Rates use decimals. Yield is a nominal annual rate compounded at coupon frequency."),))
    # Use a durable application path instead of TemporaryDirectory in production.
    with TemporaryDirectory() as directory, ResearchStore(Path(directory) / "research.sqlite") as research:
        workflow = AutonomousWorkflow(RunStore(research), default_registry())
        workflow.submit(request)
        state = workflow.run(request.run_id)
        assert state["status"] == "completed", state
        print(json.dumps(state["report"], indent=2))


if __name__ == "__main__":
    main()
