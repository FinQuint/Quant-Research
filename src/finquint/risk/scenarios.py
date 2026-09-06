from dataclasses import dataclass
from math import exp, isfinite
from typing import Callable, Iterable

from finquint.fixed_income.curves import YieldCurve


def _finite(value, name: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class ShiftedYieldCurve(YieldCurve):
    """Continuously compounded zero-rate shift over an existing curve."""
    def __init__(self, base_curve: YieldCurve, shift: Callable[[float], float]):
        if not isinstance(base_curve, YieldCurve):
            raise TypeError("base_curve must implement YieldCurve")
        if not callable(shift):
            raise TypeError("shift must be callable")
        self.base_curve, self._shift = base_curve, shift

    def discount_factor(self, maturity: float) -> float:
        base = self.base_curve.discount_factor(maturity)
        if maturity == 0:
            return base
        shift = _finite(self._shift(maturity), "zero-rate shift")
        try:
            result = base * exp(-shift * maturity)
        except OverflowError as exc:
            raise ValueError("shift produces an unrepresentable discount factor") from exc
        if not isfinite(result) or result <= 0:
            raise ValueError("shift produces an invalid discount factor")
        return result


def parallel_shift(curve: YieldCurve, shift_bps: float) -> ShiftedYieldCurve:
    shift = _finite(shift_bps, "shift_bps") / 10_000
    return ShiftedYieldCurve(curve, lambda _time: shift)


def _grid(values: Iterable[float]) -> tuple[float, ...]:
    grid = tuple(float(value) for value in values)
    if not grid or any(not isfinite(value) or value <= 0 for value in grid):
        raise ValueError("key maturities must be finite and positive")
    if any(right <= left for left, right in zip(grid, grid[1:])):
        raise ValueError("key maturities must be strictly increasing")
    return grid


def _key_weight(time: float, key: float, grid: tuple[float, ...]) -> float:
    index = grid.index(key)
    if index == 0 and time <= key:
        return 1.0
    if index == len(grid) - 1 and time >= key:
        return 1.0
    left = 0.0 if index == 0 else grid[index - 1]
    right = grid[-1] if index == len(grid) - 1 else grid[index + 1]
    if time < left or time > right:
        return 0.0
    if time <= key:
        return (time - left) / (key - left)
    return (right - time) / (right - key)


def key_rate_shift(curve: YieldCurve, key_maturity: float, shift_bps: float,
                   key_maturities: Iterable[float]) -> ShiftedYieldCurve:
    grid = _grid(key_maturities)
    key = float(key_maturity)
    if key not in grid:
        raise ValueError("key_maturity must be a member of key_maturities")
    shift = _finite(shift_bps, "shift_bps") / 10_000
    return ShiftedYieldCurve(curve, lambda time: shift * _key_weight(time, key, grid))


@dataclass(frozen=True)
class CurveScenario:
    name: str
    node_shifts_bps: tuple[tuple[float, float], ...]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("scenario name must be nonempty")
        nodes = tuple((float(time), float(shift)) for time, shift in self.node_shifts_bps)
        _grid(time for time, _ in nodes)
        if any(not isfinite(shift) for _, shift in nodes):
            raise ValueError("scenario shifts must be finite")
        object.__setattr__(self, "node_shifts_bps", nodes)

    def shift_bps(self, time: float) -> float:
        nodes = self.node_shifts_bps
        if time <= nodes[0][0]:
            return nodes[0][1]
        if time >= nodes[-1][0]:
            return nodes[-1][1]
        for (left_t, left_s), (right_t, right_s) in zip(nodes, nodes[1:]):
            if left_t <= time <= right_t:
                weight = (time - left_t) / (right_t - left_t)
                return left_s + weight * (right_s - left_s)
        raise RuntimeError("scenario interpolation failed")

    def apply(self, curve: YieldCurve) -> ShiftedYieldCurve:
        return ShiftedYieldCurve(curve, lambda time: self.shift_bps(time) / 10_000)


def steepener_scenario(short_bps: float = -25, long_bps: float = 25,
                       short_maturity: float = 2, long_maturity: float = 10) -> CurveScenario:
    return CurveScenario("steepener", ((short_maturity, short_bps), (long_maturity, long_bps)))


def flattener_scenario(short_bps: float = 25, long_bps: float = -25,
                       short_maturity: float = 2, long_maturity: float = 10) -> CurveScenario:
    return CurveScenario("flattener", ((short_maturity, short_bps), (long_maturity, long_bps)))


def twist_scenario(short_bps: float = 20, belly_bps: float = 0, long_bps: float = -20,
                   short_maturity: float = 2, belly_maturity: float = 5,
                   long_maturity: float = 10) -> CurveScenario:
    return CurveScenario("twist", ((short_maturity, short_bps), (belly_maturity, belly_bps), (long_maturity, long_bps)))

