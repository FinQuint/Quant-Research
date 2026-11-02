"""Hard invariants checked on every backtest (ADR-005).

- No lookahead: decisions made up to a cutoff must not change when every bar after the
  cutoff is removed (a truncation test; the engine itself is unchanged).
- Point-in-time data only: reuses the 10C PointInTimeCheck.
- Frozen out-of-sample window: the split's digest must match the one frozen at task start,
  and tuning must read only the training window.
- An in-sample-only improvement is reported as not accepted.

Each check returns an InvariantResult; anything it cannot verify fails closed.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from math import isclose, isfinite
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from finquint.autonomous.models import canonical
from finquint.backtesting.core import validate_bars
from finquint.backtesting.engine import EventDrivenBacktester
from finquint.evaluation.checks import PointInTimeCheck
from finquint.evaluation.core import EvaluationContext

from .paths import (DEFAULT_PROTECTED_PATHS, PatchError, PathCheck, ProtectedPathError, action_paths,
                    changed_paths, check_action_paths, check_paths, is_protected, normalize_path, patch_paths)


@dataclass(frozen=True)
class InvariantResult:
    invariant: str
    passed: bool
    evidence: str
    details: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


def protected_paths_invariant(action, roots=DEFAULT_PROTECTED_PATHS):
    result = check_action_paths(action, roots)
    evidence = ("no protected paths touched" if result.passed else
                "; ".join(f"{path}: {reason}" for path, reason in result.violations))
    return InvariantResult("protected_paths", result.passed, evidence,
                           {"violations": [list(v) for v in result.violations]})


# --- lookahead -----------------------------------------------------------------------------

def _orders(result, cutoff=None):
    frame = result.orders
    if cutoff is not None:
        frame = frame[pd.to_datetime(frame["submitted_at"], utc=True) <= cutoff]
    return [(str(r.order_id), str(r.symbol), float(r.quantity), pd.Timestamp(r.submitted_at))
            for r in frame.itertuples(index=False)]


def _same_orders(left, right):
    if len(left) != len(right):
        return False
    for (lid, lsym, lqty, lts), (rid, rsym, rqty, rts) in zip(left, right):
        if (lid, lsym, lts) != (rid, rsym, rts) or not isclose(lqty, rqty, rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True


def _first_difference(left, right):
    for index, (a, b) in enumerate(zip(left, right)):
        if not _same_orders([a], [b]):
            return index
    return min(len(left), len(right))


def default_cutoffs(timestamps, count):
    """count cutoffs spread evenly over all bars but the last."""
    candidates = list(timestamps[:-1])
    if count < 1 or not candidates:
        return []
    if count >= len(candidates):
        return candidates
    step = (len(candidates) - 1) / max(count - 1, 1)
    return sorted({candidates[round(i * step)] for i in range(count)})


def check_lookahead(market_data, strategy_factory, *, backtester=None, cutoffs=None, cutoff_count=5):
    """Truncation test for lookahead.

    strategy_factory(market_data) must build a fresh strategy from the data it is given
    (all of a strategy's data must arrive through the factory or the engine's context).
    The backtest runs on the full data, then once per cutoff on the data up to that cutoff.
    Orders submitted up to the cutoff must be identical; any difference means a decision
    used information from after its timestamp.

    cutoff_count is a (tune) value: more cutoffs catch rarer leaks at the cost of more runs.
    """
    name = "no_lookahead"
    try:
        data = validate_bars(market_data)
    except Exception as exc:
        return InvariantResult(name, False, f"market data rejected: {exc}")
    backtester = backtester or EventDrivenBacktester()
    timestamps = list(data["timestamp"].drop_duplicates())
    points = ([pd.Timestamp(c).tz_convert("UTC") if pd.Timestamp(c).tzinfo else pd.Timestamp(c, tz="UTC")
               for c in cutoffs] if cutoffs is not None else default_cutoffs(timestamps, cutoff_count))
    if not points:
        return InvariantResult(name, False, "no cutoffs to test; need at least two bars")

    def run(frame):
        return backtester.run(frame, strategy_factory(frame.copy()))

    try:
        full = _orders(run(data))
        again = _orders(run(data))
    except Exception as exc:
        return InvariantResult(name, False, f"backtest failed: {type(exc).__name__}: {exc}")
    if not _same_orders(full, again):
        return InvariantResult(name, False, "strategy is not deterministic; lookahead cannot be verified",
                               {"cutoffs_tested": 0})
    for cutoff in points:
        truncated_data = data[data["timestamp"] <= cutoff]
        if truncated_data.empty:
            return InvariantResult(name, False, f"cutoff {cutoff} is before the first bar")
        try:
            truncated = _orders(run(truncated_data))
        except Exception as exc:
            return InvariantResult(name, False, f"backtest failed at cutoff {cutoff}: {type(exc).__name__}: {exc}")
        expected = [o for o in full if o[3] <= cutoff]
        if not _same_orders(expected, truncated):
            index = _first_difference(expected, truncated)
            at = (expected[index][3] if index < len(expected) else truncated[index][3]).isoformat()
            return InvariantResult(name, False,
                                   f"decisions up to {cutoff.isoformat()} change when later bars are removed "
                                   f"(first difference at {at}): the strategy uses future data",
                                   {"cutoff": cutoff.isoformat(), "first_difference_at": at,
                                    "orders_full": len(expected), "orders_truncated": len(truncated)})
    return InvariantResult(name, True, f"decisions unchanged at {len(points)} cutoffs",
                           {"cutoffs_tested": len(points)})


# --- point in time ---------------------------------------------------------------------------

def check_point_in_time(frame, **columns):
    """Event <= availability <= decision for every row (the 10C check, as an invariant)."""
    result = PointInTimeCheck(**columns).evaluate(EvaluationContext(frame))
    return InvariantResult("point_in_time", result.passed, result.evidence, dict(result.metrics))


# --- frozen split ----------------------------------------------------------------------------

def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


@dataclass(frozen=True)
class SplitManifest:
    """The train/test split, frozen when a task starts. Its digest is recorded then."""
    train_start: object
    train_end: object
    test_start: object
    test_end: object
    version: str = "1"

    def __post_init__(self):
        for name in ("train_start", "train_end", "test_start", "test_end"):
            object.__setattr__(self, name, _utc(getattr(self, name)))
        if not self.train_start <= self.train_end < self.test_start <= self.test_end:
            raise ValueError("split must satisfy train_start <= train_end < test_start <= test_end")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("split version is required")

    def to_dict(self):
        return {"train_start": self.train_start.isoformat(), "train_end": self.train_end.isoformat(),
                "test_start": self.test_start.isoformat(), "test_end": self.test_end.isoformat(),
                "version": self.version}

    @classmethod
    def from_dict(cls, data):
        return cls(data["train_start"], data["train_end"], data["test_start"], data["test_end"],
                   data.get("version", "1"))

    def digest(self):
        return sha256(canonical(self.to_dict()).encode()).hexdigest()


def check_frozen_split(manifest, *, frozen_digest, tuning_timestamps):
    """The split is unchanged since it was frozen, and tuning read only the training window."""
    name = "frozen_split"
    if not isinstance(manifest, SplitManifest):
        return InvariantResult(name, False, "no split manifest")
    if manifest.digest() != frozen_digest:
        return InvariantResult(name, False, "split changed after it was frozen",
                               {"expected": frozen_digest, "actual": manifest.digest()})
    try:
        stamps = pd.to_datetime(pd.Series(list(tuning_timestamps)), utc=True, errors="raise")
    except Exception as exc:
        return InvariantResult(name, False, f"tuning timestamps unreadable: {exc}")
    if stamps.isna().any():
        return InvariantResult(name, False, "tuning timestamps contain missing values")
    in_test = int(((stamps >= manifest.test_start) & (stamps <= manifest.test_end)).sum())
    outside_train = int(((stamps < manifest.train_start) | (stamps > manifest.train_end)).sum())
    if outside_train:
        return InvariantResult(name, False,
                               f"tuning read {outside_train} observations outside the training window "
                               f"({in_test} in the frozen test window)",
                               {"outside_train": outside_train, "in_test_window": in_test})
    return InvariantResult(name, True, f"tuning used {len(stamps)} training observations only",
                           {"observations": len(stamps)})


# --- acceptance ------------------------------------------------------------------------------

def judge_improvement(*, baseline_in_sample, baseline_out_of_sample, candidate_in_sample,
                      candidate_out_of_sample, min_oos_improvement=0.0, higher_is_better=True):
    """A change is accepted only if it improves out of sample by more than min_oos_improvement.

    min_oos_improvement is a (tune) value. An in-sample gain alone is never accepted.
    """
    name = "out_of_sample_improvement"
    values = (baseline_in_sample, baseline_out_of_sample, candidate_in_sample, candidate_out_of_sample,
              min_oos_improvement)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
        return InvariantResult(name, False, "metrics must be finite numbers")
    sign = 1.0 if higher_is_better else -1.0
    is_delta = sign * (candidate_in_sample - baseline_in_sample)
    oos_delta = sign * (candidate_out_of_sample - baseline_out_of_sample)
    details = {"in_sample_delta": is_delta, "out_of_sample_delta": oos_delta,
               "min_oos_improvement": float(min_oos_improvement)}
    if oos_delta > min_oos_improvement:
        return InvariantResult(name, True, "improves out of sample", details)
    if is_delta > 0:
        return InvariantResult(name, False, "not accepted: improves in sample but not out of sample", details)
    return InvariantResult(name, False, "not accepted: no out-of-sample improvement", details)


__all__ = ["DEFAULT_PROTECTED_PATHS", "InvariantResult", "PatchError", "PathCheck", "ProtectedPathError",
           "SplitManifest", "action_paths", "changed_paths", "check_action_paths", "check_frozen_split",
           "check_lookahead", "check_paths", "check_point_in_time", "default_cutoffs", "is_protected",
           "judge_improvement", "normalize_path", "patch_paths", "protected_paths_invariant"]
