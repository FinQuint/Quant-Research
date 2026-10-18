"""Bounded parallel scheduling; only the coordinator writes SQLite state."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

from .models import NeedsRevision, Planner, RetryableError, digest
from .registry import Supervisor


class ReportAgent:
    def summarize(self, state):
        # Extractive reporting cannot invent statistics or turn source prose into a conclusion.
        return {"objective": state["plan"]["objective"], "plan_digest": state["plan_digest"],
                "code_version": state["plan"]["code_version"],
                "sources": deepcopy(state["plan"]["sources"]),
                "results": {key: {"output": task["output"], "output_digest": task["output_digest"],
                                   "gates": task["gates"], "attempts": task["attempts"]}
                            for key, task in state["tasks"].items()},
                "limitations": ["Source excerpts establish provenance, not truth or predictive validity.",
                                "Only registered method-specific gates have been evaluated.",
                                "Code proposals are inert text and have not been applied or tested."]}


class AutonomousWorkflow:
    def __init__(self, store, registry, *, max_workers=4):
        if type(max_workers) is not int or not 1 <= max_workers <= 16:
            raise ValueError("max_workers must be between 1 and 16")
        self.store, self.registry, self.max_workers = store, registry, max_workers
        self.supervisor = Supervisor()

    def submit(self, request):
        return self.store.create(Planner().build(request, self.registry))

    def _check_versions(self, state):
        for name, signature in state["plan"]["methods"].items():
            if self.registry.get(name).signature() != signature:
                raise NeedsRevision(f"registered method or risk policy changed: {name}")
        if digest(state["plan"]) != state["plan_digest"]:
            raise NeedsRevision("plan digest mismatch")

    def decide(self, run_id, task_id, *, plan_digest, reviewer, approved, notes):
        if not reviewer.strip() or not notes.strip() or type(approved) is not bool:
            raise ValueError("human identity, boolean decision and notes required")
        # Approval entry point belongs exclusively to the authenticated host UI.
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            state = self.store.load(run_id)
            self._check_versions(state)
            if state["status"] != "awaiting_approval" or state["plan_digest"] != plan_digest:
                raise ValueError("approval requires a paused run and matching plan digest")
            task = state["tasks"][task_id]
            if task["status"] != "awaiting_approval":
                raise ValueError("task is not awaiting approval")
            task["approved"] = approved
            task["status"] = "pending" if approved else "rejected"
            state["status"] = "ready" if approved else "rejected"
            self.store.save(state, "autonomous_human_decision", {"task_id": task_id, "reviewer": reviewer,
                            "approved": approved, "notes": notes, "plan_digest": plan_digest})
        return state

    def recover(self, run_id, *, reviewer, notes):
        """Only after the host has stopped the old process. Pure adapters can replay."""
        if not reviewer.strip() or not notes.strip():
            raise ValueError("human identity and recovery notes required")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            state = self.store.load(run_id)
            if state["status"] != "running":
                raise ValueError("only interrupted running state can be recovered")
            for task in state["tasks"].values():
                if task["status"] == "running":
                    task["status"] = "pending"
            state["status"] = "ready"
            self.store.save(state, "autonomous_recovered", {"reviewer": reviewer, "notes": notes})
        return state

    def _execute(self, call, state):
        method = self.registry.get(call["method"])
        context = {"sources": deepcopy(state["plan"]["sources"]),
                   "dependencies": {dep: deepcopy(state["tasks"][dep]["output"]) for dep in call["dependencies"]},
                   "code_version": state["plan"]["code_version"]}
        parameters = deepcopy(call["parameters"])
        try:
            method.validate_input(deepcopy(parameters))
        except Exception as exc:
            raise ValueError(f"input validation failed: {exc}") from exc
        output = method.execute(deepcopy(parameters), deepcopy(context))
        gates = self.supervisor.validate(method, parameters, context, output)
        return deepcopy(output), gates

    def run(self, run_id):
        state, claimed = self.store.claim(run_id)
        if not claimed:
            return state
        try:
            self._check_versions(state)
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                while True:
                    ready = []
                    for call in state["plan"]["calls"]:
                        task = state["tasks"][call["task_id"]]
                        if task["status"] != "pending" or not all(state["tasks"][d]["status"] == "succeeded" for d in call["dependencies"]):
                            continue
                        method = self.registry.get(call["method"])
                        if method.signature()["requires_approval"] and not task["approved"]:
                            task["status"] = "awaiting_approval"
                        elif task["attempts"] >= state["plan"]["max_attempts"]:
                            task.update(status="failed", error="attempt budget exhausted")
                        else:
                            task.update(status="running", attempts=task["attempts"] + 1)
                            ready.append(call)
                    self.store.save(state, "autonomous_wave_started", {"tasks": [c["task_id"] for c in ready]})
                    if not ready:
                        break
                    snapshot = deepcopy(state)
                    futures = {pool.submit(self._execute, call, snapshot): call for call in ready}
                    for future in as_completed(futures):
                        call = futures[future]
                        task = state["tasks"][call["task_id"]]
                        try:
                            output, gates = future.result()
                            task.update(status="succeeded", output=output, output_digest=digest(output), gates=gates, error=None)
                        except RetryableError as exc:
                            task.update(status="pending" if task["attempts"] < state["plan"]["max_attempts"] else "failed",
                                        error=f"{type(exc).__name__}: {exc}")
                        except NeedsRevision as exc:
                            task.update(status="needs_revision", error=str(exc))
                        except Exception as exc:
                            task.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                        self.store.save(state, "autonomous_task_finished", {"task_id": call["task_id"], **task})
                    if any(t["status"] in {"failed", "needs_revision"} for t in state["tasks"].values()):
                        break
            statuses = {t["status"] for t in state["tasks"].values()}
            if "needs_revision" in statuses:
                state["status"] = "needs_revision"
            elif "failed" in statuses:
                state["status"] = "failed"
            elif "awaiting_approval" in statuses:
                state["status"] = "awaiting_approval"
            elif statuses == {"succeeded"}:
                state["report"] = ReportAgent().summarize(state)
                state["status"] = "completed"
            else:
                state["status"] = "failed"
        except NeedsRevision as exc:
            state.update(status="needs_revision", error=str(exc))
        except Exception as exc:
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        self.store.save(state, "autonomous_run_finished")
        return state
