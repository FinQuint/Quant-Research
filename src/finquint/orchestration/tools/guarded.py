"""The only path from an agent's proposal to a side effect: token check + idempotency ledger."""
from copy import deepcopy
from dataclasses import dataclass

from finquint.autonomous.models import canonical

from ..gate.capabilities import CapabilityError
from ..gate.decide import ProposedAction, fingerprint
from .ledger import EffectStatus, idempotency_key
from .registry import SideEffect


@dataclass(frozen=True)
class ToolOutcome:
    status: EffectStatus
    key: str
    result: dict | None = None
    error: str | None = None
    replayed: bool = False   # True when the ledger already held this call


def _checked_result(result):
    if not isinstance(result, dict):
        raise TypeError("tool must return a JSON object")
    canonical(result)
    return deepcopy(result)


class GuardedTools:
    def __init__(self, registry, ledger, authority):
        self.registry, self.ledger, self.authority = registry, ledger, authority

    def call(self, run_id, action, *, token=None, attempt_scope="0"):
        if not isinstance(action, ProposedAction):
            raise TypeError("expected ProposedAction")
        tool = self.registry.get(action.action_type)
        action_fp = fingerprint(action)
        key = idempotency_key(run_id, action_fp, attempt_scope)
        if tool.requires_token:
            try:
                self.authority.verify(token, tool=tool.name, action_fingerprint=action_fp)
            except CapabilityError as exc:
                self.ledger.record("tool_call_refused", run_id,
                                   {"tool": tool.name, "fingerprint": action_fp, "reason": str(exc)})
                raise

        if tool.side_effect is SideEffect.READ_ONLY:
            return ToolOutcome(EffectStatus.DONE, key, _checked_result(tool.handler(deepcopy(action), key)))

        record = dict(run_id=run_id, fingerprint=action_fp, tool=tool.name, action=action.to_dict())
        if tool.uses_outbox:
            entry, created = self.ledger.enqueue(key, **record)
            return ToolOutcome(entry.status, key, entry.result, entry.error, replayed=not created)

        entry, created = self.ledger.begin(key, **record)
        if entry.status is not EffectStatus.PENDING:
            return ToolOutcome(entry.status, key, entry.result, entry.error, replayed=True)
        try:
            result = _checked_result(tool.handler(deepcopy(action), key))
        except Exception as exc:
            entry = self.ledger.finish(key, EffectStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            return ToolOutcome(entry.status, key, None, entry.error, replayed=not created)
        entry = self.ledger.finish(key, EffectStatus.DONE, result=result)
        return ToolOutcome(entry.status, key, entry.result, replayed=not created)


class OutboxWorker:
    """Dispatches EXTERNAL side effects. Run exactly one worker per ledger.

    recover() must run only when no other worker is alive (same rule as RunStore.recover).
    """

    def __init__(self, registry, ledger):
        self.registry, self.ledger = registry, ledger

    def _action(self, entry):
        action = ProposedAction.from_dict(entry.action)
        if fingerprint(action) != entry.fingerprint:
            raise ValueError("stored action does not match its fingerprint")
        return action

    def _reconcile(self, tool, action, entry):
        """DONE result if the effect is known to have happened, False if known not to, None if unknown."""
        if tool.reconcile is None:
            return None
        try:
            found = tool.reconcile(deepcopy(action), entry.key)
            return False if found is None else _checked_result(found)
        except Exception:
            return None

    def recover(self):
        """Settle entries left dispatching by a crashed worker, without ever re-sending blindly."""
        settled = []
        for entry in self.ledger.entries(status=EffectStatus.DISPATCHING):
            tool = self.registry.get(entry.tool)
            try:
                action = self._action(entry)
            except Exception as exc:
                settled.append(self.ledger.finish(entry.key, EffectStatus.IN_DOUBT, error=str(exc)))
                continue
            found = self._reconcile(tool, action, entry)
            if isinstance(found, dict):
                settled.append(self.ledger.finish(entry.key, EffectStatus.DONE, result=found))
            elif found is False:
                self.ledger.requeue(entry.key)
                settled.append(self.ledger.get(entry.key))
            else:
                settled.append(self.ledger.finish(
                    entry.key, EffectStatus.IN_DOUBT,
                    error="dispatch interrupted and outcome cannot be confirmed; escalate"))
        return tuple(settled)

    def run_once(self):
        entry = self.ledger.claim_next()
        if entry is None:
            return None
        tool = self.registry.get(entry.tool)
        try:
            action = self._action(entry)
        except Exception as exc:
            return self.ledger.finish(entry.key, EffectStatus.FAILED, error=str(exc))  # never sent
        try:
            result = _checked_result(tool.handler(deepcopy(action), entry.key))
        except Exception as exc:
            # The call may have landed before failing; only reconcile can say it did not.
            found = self._reconcile(tool, action, entry)
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(found, dict):
                return self.ledger.finish(entry.key, EffectStatus.DONE, result=found)
            if found is False:
                return self.ledger.finish(entry.key, EffectStatus.FAILED, error=error)
            return self.ledger.finish(entry.key, EffectStatus.IN_DOUBT, error=error)
        return self.ledger.finish(entry.key, EffectStatus.DONE, result=result)

    def drain(self, *, limit=100):
        processed = []
        while len(processed) < limit:
            entry = self.run_once()
            if entry is None:
                break
            processed.append(entry)
        return tuple(processed)
