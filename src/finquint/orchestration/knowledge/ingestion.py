"""Event-driven re-indexing (spec: "Knowledge freshness").

Events are explicit calls, so they are deterministic and testable; a webhook or CI job can call
the same methods later.

    on_code_sha(sha)                 new code: re-index symbols, new evidence versions for changed
                                     code, demote summaries of changed files
    on_experiment_verified(record)   a verified experiment becomes authoritative evidence
    on_dataset_version(name, ver)    a provider update: demote derived knowledge on the old version
    on_run_completed(state)          a completed autonomous run's succeeded outputs become
                                     authoritative evidence (artifact run:RUN/TASK)

Old versions are never deleted. The last seen code SHA and dataset versions are kept in SQLite
(knowledge_state), so a restart does not re-announce old events.
"""
from dataclasses import dataclass, replace
import json

from ..context.evidence import Evidence, current_items
from .code_index import CodeIndex
from .evidence_store import demote_superseded


@dataclass(frozen=True)
class IngestReport:
    event: str
    added: tuple = ()        # evidence refs written
    demoted: tuple = ()      # refs of new provisional (demoted) versions
    changed_files: tuple = ()
    skipped: bool = False    # the event had already been seen
    deprecated: tuple = ()   # refs of code that no longer exists


class Ingestor:
    def __init__(self, store, connection, *, code_root=None, package="src/finquint"):
        self.store = store
        self.db = connection
        self.code_root, self.package = code_root, package
        self.db.execute("CREATE TABLE IF NOT EXISTS knowledge_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.index = None

    def _get(self, key):
        row = self.db.execute("SELECT value FROM knowledge_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO knowledge_state VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                            (key, json.dumps(value, sort_keys=True)))

    # --- code ------------------------------------------------------------------------------------

    def on_code_sha(self, sha):
        if self.code_root is None:
            raise ValueError("on_code_sha needs code_root")
        previous = self._get("code")
        if previous and previous["sha"] == sha:
            return IngestReport("code_sha", skipped=True)
        index = CodeIndex.build(self.code_root, sha, package=self.package)
        changed = index.changed_files(CodeIndex(previous["sha"], (), previous["file_hashes"])) if previous \
            else sorted(index.file_hashes)
        changed_set = set(changed)
        current = {item.evidence_id: item for item in current_items(self.store)}
        added, kept = [], set()
        for item in index.evidence():
            kept.add(item.evidence_id)
            if item.claims["path"] in changed_set or item.evidence_id not in current:
                self.store.put(item)
                added.append(item.ref)
        # Code that no longer exists: its last version is deprecated, so claims resting on it demote.
        deprecated = []
        for item in current.values():
            if (item.topic == "code" and item.source.startswith("file:") and item.source[5:] in changed_set
                    and item.evidence_id not in kept and not item.deprecated):
                self.store.put(replace(item, deprecated=True))
                deprecated.append(item.ref)
        # Summaries of changed files: anything sourced from file:PATH that is not code itself.
        demoted = []
        for item in current.values():
            path = item.source[5:] if item.source.startswith("file:") else None
            if path in changed_set and item.topic != "code" and item.tier in ("verified", "provisional") \
                    and not item.version.endswith(".stale"):
                stale = replace(item, version=f"{item.version}.stale", tier="provisional",
                                source=f"{item.source}; stale at {sha[:12]}")
                self.store.put(stale)
                demoted.append(stale.ref)
        demoted += demote_superseded(self.store)
        self.index = index
        self._set("code", {"sha": sha, "file_hashes": index.file_hashes})
        return IngestReport("code_sha", tuple(added), tuple(demoted), tuple(changed), deprecated=tuple(deprecated))

    # --- results and data ------------------------------------------------------------------------

    def on_experiment_verified(self, record, *, topic="experiment"):
        """record: ExperimentRecord (or its dict). Its metrics become authoritative evidence."""
        data = record if isinstance(record, dict) else {k: getattr(record, k) for k in record.__dataclass_fields__}
        versions = dict(data.get("dataset_versions") or {})
        dataset = next((f"{k}@{v}" for k, v in sorted(versions.items())), None)
        item = Evidence(f"experiment-{data['experiment_id']}", "1", topic,
                        json.dumps({"metrics": dict(data.get("metrics") or {}),
                                    "parameters": dict(data.get("parameters") or {})}, sort_keys=True),
                        run_id=None, dataset=dataset, source=f"experiment:{data['experiment_id']}",
                        claims={k: v for k, v in (data.get("metrics") or {}).items()})
        self.store.put(item)
        return IngestReport("experiment_verified", (item.ref,), tuple(demote_superseded(self.store)))

    def on_dataset_version(self, name, version):
        known = self._get("datasets") or {}
        if known.get(name) == version:
            return IngestReport("dataset_version", skipped=True)
        demoted = []
        for item in current_items(self.store):
            if item.dataset_name == name and item.dataset_version != version and item.tier == "verified":
                stale = replace(item, version=f"{item.version}.stale", tier="provisional",
                                source=f"{item.source}; {name} moved to {version}")
                self.store.put(stale)
                demoted.append(stale.ref)
        known[name] = version
        self._set("datasets", known)
        return IngestReport("dataset_version", (), tuple(demoted))

    def on_run_completed(self, state):
        """Succeeded task outputs of a completed run become authoritative evidence."""
        if state.get("status") != "completed":
            return IngestReport("run_completed", skipped=True)
        run_id = state["plan"]["run_id"]
        added = []
        for task_id, task in sorted(state["tasks"].items()):
            if task["status"] != "succeeded":
                continue
            item = Evidence(f"run-{run_id}-{task_id}", task["output_digest"][:12], task_id,
                            json.dumps(task["output"], sort_keys=True), run_id=run_id,
                            source=f"run:{run_id}/{task_id}")
            self.store.put(item)
            added.append(item.ref)
        return IngestReport("run_completed", tuple(added))

