from math import isfinite
from statistics import fmean, pstdev

import pandas as pd

from .core import CheckResult, EvaluationContext, QuantResearchCheck, Severity


class PointInTimeCheck(QuantResearchCheck):
    check_id = "point_in_time_integrity"

    def __init__(self, *, event="event_time", availability="availability_time", decision="decision_time"):
        self.event, self.availability, self.decision = event, availability, decision

    def evaluate(self, context):
        required = (self.event, self.availability, self.decision)
        missing = [column for column in required if column not in context.frame]
        if missing:
            return CheckResult(self.check_id, False, Severity.ERROR,
                               f"Missing columns: {', '.join(missing)}",
                               "Record event, availability, and decision timestamps.")
        try:
            event, availability, decision = (pd.to_datetime(context.frame[column], utc=True, errors="raise") for column in required)
            violations = int(((event > availability) | (availability > decision)).sum())
        except Exception as exc:
            return CheckResult(self.check_id, False, Severity.ERROR, f"Timestamp parsing failed: {exc}",
                               "Use parseable timezone-aware timestamps.")
        return CheckResult(self.check_id, violations == 0, Severity.ERROR,
                           f"Found {violations} point-in-time ordering violations.",
                           "Exclude information unavailable at the decision timestamp.",
                           {"violations": violations, "rows": len(context.frame)})


class DataQualityCheck(QuantResearchCheck):
    check_id = "data_quality"

    def __init__(self, *, key_columns=(), max_missing_fraction=0.0):
        self.key_columns = tuple(key_columns)
        self.max_missing_fraction = float(max_missing_fraction)
        if not 0 <= self.max_missing_fraction <= 1:
            raise ValueError("max_missing_fraction must be between zero and one")

    def evaluate(self, context):
        if context.frame.empty:
            return CheckResult(self.check_id, False, Severity.ERROR, "Dataset is empty.", "Provide observations.")
        missing_columns = [column for column in self.key_columns if column not in context.frame]
        if missing_columns:
            return CheckResult(self.check_id, False, Severity.ERROR,
                               f"Missing key columns: {', '.join(missing_columns)}", "Provide all declared keys.")
        missing_fraction = float(context.frame.isna().sum().sum() / context.frame.size)
        duplicates = int(context.frame.duplicated(subset=list(self.key_columns) or None).sum())
        passed = missing_fraction <= self.max_missing_fraction and duplicates == 0
        return CheckResult(self.check_id, passed, Severity.ERROR,
                           f"Missing fraction={missing_fraction:.6f}; duplicate rows={duplicates}.",
                           "Resolve missing values explicitly and remove or explain duplicates.",
                           {"missing_fraction": missing_fraction, "duplicate_rows": duplicates})


class OutOfSampleCheck(QuantResearchCheck):
    check_id = "out_of_sample_design"

    def __init__(self, *, split_column="sample", minimum_test_observations=20):
        self.split_column, self.minimum = split_column, int(minimum_test_observations)
        if self.minimum < 1:
            raise ValueError("minimum_test_observations must be positive")

    def evaluate(self, context):
        if self.split_column not in context.frame:
            return CheckResult(self.check_id, False, Severity.ERROR, "No frozen train/test split is recorded.",
                               "Add a sample column with train and test observations.")
        values = context.frame[self.split_column].astype(str).str.lower()
        train, test = int((values == "train").sum()), int((values == "test").sum())
        passed = train > 0 and test >= self.minimum
        return CheckResult(self.check_id, passed, Severity.ERROR,
                           f"Train observations={train}; test observations={test}.",
                           "Freeze a nonempty training sample and sufficient untouched test sample.",
                           {"train_observations": train, "test_observations": test})


class BenchmarkCheck(QuantResearchCheck):
    check_id = "benchmark_declared"

    def evaluate(self, context):
        benchmark = str(context.metadata.get("benchmark", "")).strip()
        return CheckResult(self.check_id, bool(benchmark), Severity.ERROR,
                           f"Benchmark={benchmark or 'missing'}.", "Declare an economically relevant benchmark.")


class TransactionCostCheck(QuantResearchCheck):
    check_id = "transaction_costs"

    def evaluate(self, context):
        turnover = context.metadata.get("annual_turnover")
        cost = context.metadata.get("transaction_cost_bps")
        valid = all(value is not None and isfinite(float(value)) and float(value) >= 0 for value in (turnover, cost))
        passed = valid and (float(turnover) == 0 or float(cost) > 0)
        evidence = "Turnover or transaction costs are missing/invalid." if not valid else f"Annual turnover={turnover}; cost={cost} bps."
        return CheckResult(self.check_id, passed, Severity.ERROR, evidence,
                           "Record turnover and apply a positive cost assumption when trading occurs.")


class MultipleTestingCheck(QuantResearchCheck):
    check_id = "multiple_testing"

    def evaluate(self, context):
        variants = context.metadata.get("tested_variants")
        method = str(context.metadata.get("multiple_testing_correction", "")).strip().lower()
        valid = isinstance(variants, int) and not isinstance(variants, bool) and variants >= 1
        passed = valid and (variants == 1 or method in {"bonferroni", "holm", "fdr"})
        return CheckResult(self.check_id, passed, Severity.ERROR,
                           f"Tested variants={variants}; correction={method or 'missing'}.",
                           "Record every tested variant and use an approved multiplicity correction.")


class SubperiodStabilityCheck(QuantResearchCheck):
    check_id = "subperiod_stability"

    def __init__(self, *, maximum_dispersion=1.0):
        self.maximum_dispersion = float(maximum_dispersion)
        if not isfinite(self.maximum_dispersion) or self.maximum_dispersion < 0:
            raise ValueError("maximum_dispersion must be finite and nonnegative")

    def evaluate(self, context):
        raw = context.metadata.get("subperiod_metrics", ())
        try:
            values = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            values = ()
        if len(values) < 3 or any(not isfinite(value) for value in values):
            return CheckResult(self.check_id, False, Severity.WARNING,
                               "Fewer than three finite subperiod metrics are available.",
                               "Evaluate at least three economically distinct subperiods.")
        mean = fmean(values)
        dispersion = pstdev(values) / max(abs(mean), 1e-12)
        return CheckResult(self.check_id, dispersion <= self.maximum_dispersion, Severity.WARNING,
                           f"Relative subperiod dispersion={dispersion:.6f}.",
                           "Investigate regime dependence and unstable parameters.",
                           {"relative_dispersion": dispersion, "subperiods": len(values)})
