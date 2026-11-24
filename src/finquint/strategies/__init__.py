"""Reviewed strategy templates: named strategy code with typed, bounded parameters (spec, Phase 9).

The planner agent chooses a template and its parameters; it never writes strategy code. New
templates arrive through a reviewed PR: the coding agent (Phase 10) writes a new module and the
host registers it in agent_templates.py.
"""
from types import MappingProxyType

from .agent_templates import AGENT_TEMPLATES
from .base import Param, StrategyTemplate
from .momentum import TimeSeriesMomentum


TEMPLATES = MappingProxyType({
    "ts_momentum": StrategyTemplate(
        "ts_momentum",
        "Time-series momentum on one symbol: hold +units when the close-to-close return over the last "
        "`lookback` bars is positive, -units (or flat when long_only) when it is negative.",
        {"lookback": Param("integer", 5, 250, 60), "units": Param("number", 1, 10_000, 5000),
         "long_only": Param("boolean", default=False)},
        TimeSeriesMomentum),
    **{t.name: t for t in AGENT_TEMPLATES},
})
if len(TEMPLATES) != 1 + len(AGENT_TEMPLATES):
    raise ImportError("an agent-written template reuses a registered template name")


def template(name):
    if name not in TEMPLATES:
        raise ValueError(f"strategy template is not registered: {name}")
    return TEMPLATES[name]


__all__ = ["Param", "StrategyTemplate", "TEMPLATES", "TimeSeriesMomentum", "template"]
