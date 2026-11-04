"""Model socket: one interface between the workflow and any language model (Phase 5).

Plugs implement ModelPlug. The core (ModelCaller) validates, repairs once, escalates,
falls back and maps errors the same way for every plug. Plugs shipped here: SimulatedModel
(deterministic, the CI gate) and RecordingPlug / ReplayPlug.
"""
from .conformance import check_conformance
from .core import MAX_REPAIRS, ModelCaller, ModelEscalation, ModelResult, check_output, parse_json
from .replay import RecordingPlug, ReplayMiss, ReplayPlug
from .schema import SchemaError, validate
from .simulated import SimulatedModel
from .socket import (SOCKET_VERSION, Capabilities, CountsTokens, IncompatiblePlug, ModelPlug, ModelRefused,
                     ModelReply, ModelRequest, ModelUnavailable, Segment, SegmentRole, Usage, check_plug)
from .tokens import Calibration, CharEstimate, TokenCounter

__all__ = ["MAX_REPAIRS", "SOCKET_VERSION", "Calibration", "Capabilities", "CharEstimate", "CountsTokens",
           "IncompatiblePlug", "ModelCaller", "ModelEscalation", "ModelPlug", "ModelRefused", "ModelReply",
           "ModelRequest", "ModelResult", "ModelUnavailable", "RecordingPlug", "ReplayMiss", "ReplayPlug",
           "SchemaError", "Segment", "SegmentRole", "SimulatedModel", "TokenCounter", "Usage", "check_conformance",
           "check_output", "check_plug", "parse_json", "validate"]
