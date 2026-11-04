"""Token counting without a model tokenizer.

A plug with an exact tokenizer (CountsTokens) is used as is. Otherwise the count is a
characters/4 estimate, corrected by what the provider reported on earlier calls
(Calibration) and padded by a safety margin (tune). Budgets elsewhere are percentages of
the plug's context window, so one threshold works for every model.
"""
from dataclasses import dataclass
import math
import threading

from .socket import CountsTokens

DEFAULT_CHARS_PER_TOKEN = 4.0     # (tune)
DEFAULT_SAFETY_MARGIN = 0.10      # (tune) extra fraction added to an estimate


@dataclass(frozen=True)
class CharEstimate:
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN

    def __post_init__(self):
        if self.chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")

    def count(self, text):
        return math.ceil(len(text) / self.chars_per_token) if text else 0


class Calibration:
    """Ratio of reported to estimated input tokens, per model.

    The highest ratio seen for a model is used, so the correction errs toward overcounting.
    """

    def __init__(self):
        self._ratios = {}
        self._lock = threading.Lock()

    def observe(self, model, estimated, reported):
        if estimated <= 0 or reported <= 0:
            return
        with self._lock:
            self._ratios[model] = max(self._ratios.get(model, 0.0), reported / estimated)

    def ratio(self, model):
        with self._lock:
            return self._ratios.get(model, 1.0)

    def snapshot(self):
        with self._lock:
            return dict(self._ratios)


class TokenCounter:
    """count(text) for one plug: exact when the plug can, else a calibrated, padded estimate."""

    def __init__(self, plug=None, *, estimate=None, calibration=None, margin=DEFAULT_SAFETY_MARGIN):
        if margin < 0:
            raise ValueError("margin must not be negative")
        self.plug = plug
        self.estimate = estimate or CharEstimate()
        self.calibration = calibration
        self.margin = margin

    @property
    def exact(self):
        return (self.plug is not None and self.plug.capabilities.exact_token_count
                and isinstance(self.plug, CountsTokens))

    def raw_estimate(self, text):
        return self.estimate.count(text)

    def count(self, text):
        if self.exact:
            return self.plug.count_tokens(text)
        ratio = self.calibration.ratio(self.plug.name) if (self.calibration and self.plug) else 1.0
        return math.ceil(self.estimate.count(text) * ratio * (1 + self.margin))
