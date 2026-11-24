"""Planning layer (roadmap Phase 9, ADR-022).

A planner agent proposes a plan from a host-owned catalog (planner methods, strategy templates,
pinned datasets); deterministic plan checks (verify/plans.py) and a blind plan review decide whether
it runs. Human-approved plan templates run with no model call. An accepted plan is an ordinary
Request on the existing path.
"""
from .catalog import (CONFIG_PATH, DatasetCatalog, DatasetEntry, DatasetError, FileDatasetCatalog, WINDOWS,
                      parse_ref)
from .methods import make_methods, planning_registry
from .planner import (METHOD_DOCS, PLAN_ESCALATION, PLAN_SCHEMA, PLANNER_SYSTEM, PlanningService, PlanOutcome,
                      catalog_view, fill_template, plan_shape, planner_request, review_request, trusted_evidence)

__all__ = ["CONFIG_PATH", "DatasetCatalog", "DatasetEntry", "DatasetError", "FileDatasetCatalog", "METHOD_DOCS",
           "PLAN_ESCALATION", "PLAN_SCHEMA", "PLANNER_SYSTEM", "PlanOutcome", "PlanningService", "WINDOWS",
           "catalog_view", "fill_template", "make_methods", "parse_ref", "plan_shape", "planner_request",
           "planning_registry", "review_request", "trusted_evidence"]
