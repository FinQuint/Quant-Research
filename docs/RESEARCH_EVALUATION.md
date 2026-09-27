# Adversarial quantitative-research evaluation

Phase 10C adds a deterministic gate between a research result and human
approval. It is intentionally skeptical: a profitable backtest is not accepted
unless its data timing, experiment design, benchmark, and implementation costs
are recorded and pass explicit checks.

## Included checks

| Check | Level | Purpose |
|---|---|---|
| `point_in_time_integrity` | error | Rejects event/availability/decision timestamp inversions. |
| `data_quality` | error | Rejects undeclared missing data and duplicate research keys. |
| `out_of_sample_design` | error | Requires a frozen train/test split and a minimum test sample. |
| `benchmark_declared` | error | Requires an economically relevant benchmark. |
| `transaction_costs` | error | Requires turnover and a positive cost when trading occurs. |
| `multiple_testing` | error | Records tested variants and requires an approved correction. |
| `subperiod_stability` | warning | Flags unstable results across at least three regimes. |

An error-level failure makes `EvaluationReport.passed` false. A warning remains
visible but does not automatically reject a study. This distinction prevents a
subjective stability threshold from silently overriding an otherwise auditable
research design.

## Pipeline gate

```python
stage = QuantResearchEvaluationStage(suite, metadata=metadata,
                                      fail_on_error=True)
result = QuantPipeline("research_gate").add(stage).run(research_frame)
```

The full report is stored at
`result.context.results["research_evaluation"]`. With `fail_on_error=True`, the
pipeline stops before downstream promotion when a blocking check fails.

## Controlled-agent integration

`build_agent_validation()` converts a suite and frozen input into a named Phase
10B validation. Register that callable in `AgentExecutionContext.validations`
and grant only `RUN_VALIDATIONS`. The agent can run the approved evaluation but
cannot replace the data, thresholds, or check registry through that interface.

This gate complements—never replaces—the Phase 10A human approval step. Reviewers
should inspect evidence, assumptions, code changes, dataset versions, and failed
warnings before approving a research task.

## Limits

These checks cannot prove that a dataset is truly point-in-time, choose a valid
benchmark, detect every form of leakage, or establish economic significance.
They also do not perform walk-forward testing, portfolio construction, or live
execution simulation. Those require documented domain judgment and independent
review. The inputs remain research artifacts, not investment advice.
