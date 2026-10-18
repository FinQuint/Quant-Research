"""Durable coordinator state, sharing the existing research audit database."""
import json
from .models import canonical, digest


class RunStore:
    def __init__(self, research_store):
        self.research = research_store
        self.db = research_store.connection
        self.db.execute("CREATE TABLE IF NOT EXISTS autonomous_runs (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()

    def create(self, plan):
        state = {"plan": plan, "plan_digest": digest(plan), "status": "ready", "report": None,
                 "tasks": {c["task_id"]: {"status": "pending", "attempts": 0, "output": None,
                                          "approved": False, "gates": [], "error": None}
                           for c in plan["calls"]}}
        with self.db:
            self.db.execute("INSERT INTO autonomous_runs VALUES (?,?)", (plan["run_id"], canonical(state)))
            self.research._event("autonomous_planned", plan["run_id"], state)
        return state

    def load(self, run_id):
        row = self.db.execute("SELECT payload FROM autonomous_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def save(self, state, event, details=None):
        with self.db:
            self.db.execute("UPDATE autonomous_runs SET payload=? WHERE run_id=?",
                            (canonical(state), state["plan"]["run_id"]))
            self.research._event(event, state["plan"]["run_id"], details or {"status": state["status"]})

    def claim(self, run_id):
        # BEGIN IMMEDIATE serializes claims across processes/connections.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.load(run_id)
            if state["status"] == "running":
                raise RuntimeError("run already active; interrupted runs require human recovery")
            if state["status"] in {"completed", "failed", "needs_revision", "rejected"}:
                return state, False
            state["status"] = "running"
            self.db.execute("UPDATE autonomous_runs SET payload=? WHERE run_id=?", (canonical(state), run_id))
            self.research._event("autonomous_claimed", run_id, {})
        return state, True
