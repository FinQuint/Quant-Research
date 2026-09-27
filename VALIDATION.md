# Validation report

Validated on 2026-09-27 from the Phase 10B main-branch baseline
`ef59e3afdd24bc57786ebadbb85aea83fb032827`.

## Results

- Full test suite: **147 passed**
- Phase 10C example: **passed**
- Python bytecode compilation for `src` and `examples`: **passed**
- Supported CI matrix: Python 3.10, 3.11, and 3.12

Commands used:

```text
pytest -q
python examples/adversarial_research_evaluation.py
python -m compileall -q src examples
```

## Phase 10C coverage

- clean research design passes all seven checks
- future information at decision time is rejected
- missing values and duplicate research keys are rejected
- insufficient out-of-sample data is rejected
- missing or implausibly zero transaction costs are rejected
- multiple variants require an approved correction
- subperiod instability is retained as a non-blocking warning
- pipeline reports are published and can block downstream execution
- evaluation suites run through the Phase 10B approved-validation boundary

The synthetic example demonstrates software behavior only. It is not validation
of a trading strategy, market dataset, or investment result.
