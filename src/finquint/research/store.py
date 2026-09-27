"""SQLite-backed research metadata and append-only audit events."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .artifacts import ArtifactRecord
from .models import (
    AgentResult, ApprovalDecision, ExperimentRecord, ResearchPlan, ResearchTask,
    RetrospectiveProposal, TaskStatus,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class ResearchStore:
    def __init__(self, path):
        self.path = str(Path(path))
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _create_schema(self):
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY, hypothesis TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(plan_id),
            payload TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(task_id),
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(task_id),
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retrospectives (
            proposal_id TEXT PRIMARY KEY, source_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
            subject_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        self.connection.commit()

    def _event(self, event_type, subject_id, payload):
        self.connection.execute(
            "INSERT INTO audit_events(event_type,subject_id,payload,created_at) VALUES(?,?,?,?)",
            (event_type, subject_id, _json(payload), _now()),
        )

    def register_plan(self, plan: ResearchPlan):
        with self.connection:
            self.connection.execute("INSERT INTO plans VALUES(?,?,?)", (plan.plan_id, plan.hypothesis, _now()))
            for task in plan.tasks:
                self.connection.execute(
                    "INSERT INTO tasks VALUES(?,?,?,?,?)",
                    (task.task_id, plan.plan_id, _json(task.to_dict()), task.status.value, _now()),
                )
            self._event("plan_registered", plan.plan_id, {"task_ids": [task.task_id for task in plan.tasks]})

    def get_task(self, task_id: str) -> ResearchTask:
        row = self.connection.execute("SELECT payload,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        data = json.loads(row["payload"])
        return ResearchTask(
            data["task_id"], data["objective"], tuple(data["acceptance_criteria"]),
            tuple(data["dependencies"]), data["assigned_agent"],
            data["requires_human_approval"], TaskStatus(row["status"]),
        )

    def tasks(self, plan_id: str) -> tuple[ResearchTask, ...]:
        rows = self.connection.execute("SELECT task_id FROM tasks WHERE plan_id=? ORDER BY rowid", (plan_id,)).fetchall()
        return tuple(self.get_task(row["task_id"]) for row in rows)

    def set_status(self, task_id: str, status: TaskStatus, *, details=None):
        task = self.get_task(task_id)
        payload = replace(task, status=status).to_dict()
        with self.connection:
            self.connection.execute("UPDATE tasks SET payload=?,status=?,updated_at=? WHERE task_id=?",
                                    (_json(payload), status.value, _now(), task_id))
            self._event("task_status_changed", task_id,
                        {"from": task.status.value, "to": status.value, "details": details or {}})

    def save_result(self, result: AgentResult):
        self.get_task(result.task_id)
        with self.connection:
            self.connection.execute("INSERT INTO task_results(task_id,payload,created_at) VALUES(?,?,?)",
                                    (result.task_id, _json(asdict(result)), _now()))
            self._event("agent_result_recorded", result.task_id, asdict(result))

    def save_approval(self, decision: ApprovalDecision):
        self.get_task(decision.task_id)
        with self.connection:
            self.connection.execute("INSERT INTO approvals(task_id,payload,created_at) VALUES(?,?,?)",
                                    (decision.task_id, _json(asdict(decision)), _now()))
            self._event("human_decision_recorded", decision.task_id, asdict(decision))

    def register_experiment(self, record: ExperimentRecord):
        with self.connection:
            self.connection.execute("INSERT INTO experiments VALUES(?,?,?)",
                                    (record.experiment_id, _json(asdict(record)), _now()))
            self._event("experiment_registered", record.experiment_id, asdict(record))

    def register_artifact(self, record: ArtifactRecord):
        with self.connection:
            self.connection.execute("INSERT INTO artifacts VALUES(?,?,?,?)",
                                    (record.artifact_id, record.experiment_id, _json(asdict(record)), _now()))
            self._event("artifact_registered", record.artifact_id, asdict(record))

    def register_retrospective(self, proposal: RetrospectiveProposal):
        with self.connection:
            self.connection.execute("INSERT INTO retrospectives VALUES(?,?,?,?)",
                                    (proposal.proposal_id, proposal.source_task_id, _json(asdict(proposal)), _now()))
            self._event("retrospective_proposed", proposal.proposal_id, asdict(proposal))

    def audit_log(self, *, subject_id: str | None = None) -> tuple[dict, ...]:
        query, parameters = "SELECT * FROM audit_events", ()
        if subject_id is not None:
            query, parameters = query + " WHERE subject_id=?", (subject_id,)
        rows = self.connection.execute(query + " ORDER BY sequence", parameters).fetchall()
        return tuple({"sequence": row["sequence"], "event_type": row["event_type"],
                      "subject_id": row["subject_id"], "payload": json.loads(row["payload"]),
                      "created_at": row["created_at"]} for row in rows)
