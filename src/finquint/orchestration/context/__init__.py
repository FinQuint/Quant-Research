"""Context layer (roadmap Phase 5): task card, request builder, retrieval, health triggers, rebuild.

Deterministic code builds every model request from a pinned task card plus selected
evidence (ADR-001, ADR-006). Models are reached only through orchestration.models.
"""
from .builder import (CONFIG_PATH, BuiltContext, ContextBuilder, ContextOverflow, ContextPolicy, HistoryEntry,
                      render_evidence)
from .evidence import TIERS, Evidence, EvidenceStore, InMemoryEvidenceStore, SQLiteEvidenceStore
from .handoff import HANDOFF_SCHEMA_VERSION, Handoff
from .health import (Action, Finding, HealthReport, LoopDetector, Signal, check_output, check_request,
                     evidence_conflicts)
from .retrieval import Retrieved, Retriever, Selection, SubQuestion, ineligible_reason
from .session import AskResult, ContextSession, InMemoryContextArchive, SQLiteContextArchive
from .tags import REPORT_SCHEMA, parse_line
from .task_card import AcceptanceCriterion, TaskCard

__all__ = ["CONFIG_PATH", "HANDOFF_SCHEMA_VERSION", "REPORT_SCHEMA", "TIERS", "AcceptanceCriterion", "Action",
           "AskResult", "BuiltContext", "ContextBuilder", "ContextOverflow", "ContextPolicy", "ContextSession",
           "Evidence", "EvidenceStore", "Finding", "Handoff", "HealthReport", "HistoryEntry", "InMemoryContextArchive",
           "InMemoryEvidenceStore", "LoopDetector", "Retrieved", "Retriever", "SQLiteContextArchive",
           "SQLiteEvidenceStore", "Selection", "Signal", "SubQuestion", "TaskCard", "check_output", "check_request",
           "evidence_conflicts", "ineligible_reason", "parse_line", "render_evidence"]
