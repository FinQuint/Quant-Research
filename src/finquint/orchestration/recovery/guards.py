"""Guards: deterministic checks on a repair proposal before anything runs (ADR-020).

A proposal passes only if:
  * its method is the original or one of the rule's alternatives, registered, with the same role;
  * every changed parameter path is tunable and within bounds; everything else (frozen paths
    included) is byte-identical to the original call;
  * the target method's own input validator accepts the parameters;
  * its fingerprint differs from the original call and every earlier attempt (novelty);
  * the reflection's evidence is quoted verbatim from the failure packet, and its claimed changes
    equal the computed diff (a reflection cannot misdescribe what it did);
  * the protected-path invariant holds for the revised action.
"""
from dataclasses import dataclass

from finquint.autonomous.models import canonical, digest

from ..gate.decide import GatePolicy, ProposedAction
from ..verify.paths import DEFAULT_PROTECTED_PATHS

MIN_EVIDENCE_CHARS = 8


def flatten(params, prefix=""):
    """{dotted path: leaf}. Objects are walked; lists and scalars are leaves."""
    out = {}
    for key in sorted(params):
        path = f"{prefix}{key}"
        value = params[key]
        if isinstance(value, dict) and value:
            out.update(flatten(value, path + "."))
        else:
            out[path] = value
    return out


def param_diff(original, proposed):
    """Sorted parameter paths that were added, removed or changed."""
    a, b = flatten(original), flatten(proposed)
    return tuple(sorted(p for p in set(a) | set(b) if p not in a or p not in b or canonical(a[p]) != canonical(b[p])))


def call_fingerprint(method, parameters):
    return digest([method, parameters])


@dataclass(frozen=True)
class GuardResult:
    problems: tuple
    diff: tuple            # changed parameter paths
    swapped: bool          # method differs from the original
    fingerprint: str

    @property
    def passed(self):
        return not self.problems

    @property
    def changes(self):
        return tuple(sorted(self.diff + (("method",) if self.swapped else ())))


def check_proposal(original_call, proposal, reflection, *, rule, registry, earlier=(), packet_text="",
                   protected_paths=DEFAULT_PROTECTED_PATHS):
    """original_call: the plan's call dict. proposal: {"method", "parameters"}. reflection: the
    reply's reflection object. earlier: fingerprints of earlier attempts. packet_text: the exact
    failure text the agent was shown (gate results and errors)."""
    problems = []
    method, params = proposal.get("method"), proposal.get("parameters")
    if not isinstance(params, dict):
        return GuardResult(("parameters must be an object",), (), False, "")
    original_method = original_call["method"]
    swapped = method != original_method
    if swapped and method not in rule.alternatives:
        problems.append(f"method {method!r} is not an allowed alternative to {original_method!r}")
    try:
        target = registry.get(method)
        if target.role != registry.get(original_method).role:
            problems.append(f"method {method!r} has a different role")
        else:
            try:
                target.validate_input(canonical_copy(params))
            except Exception as exc:
                problems.append(f"input rejected by {method}: {exc}")
    except ValueError:
        problems.append(f"method {method!r} is not registered")
    diff = param_diff(original_call["parameters"], params)
    proposed = flatten(params)
    for path in diff:
        if path in rule.frozen:
            problems.append(f"changes frozen parameter {path!r}")
        elif path not in rule.tunable:
            problems.append(f"changes non-tunable parameter {path!r}")
        elif path not in proposed:
            problems.append(f"removes tunable parameter {path!r}")
        elif not rule.tunable[path].allows(proposed[path]):
            problems.append(f"{path}={canonical(proposed[path])} is outside its bounds {canonical(rule.tunable[path].to_dict())}")
    fingerprint = call_fingerprint(method, params)
    if fingerprint == call_fingerprint(original_method, original_call["parameters"]):
        problems.append("proposal repeats the original call")
    elif fingerprint in earlier:
        problems.append("proposal repeats an earlier attempt")
    result = GuardResult((), diff, swapped, fingerprint)
    problems += reflection_problems(reflection, result.changes, packet_text)
    action = ProposedAction(str(method), f"repair:{original_call['task_id']}", params)
    policy = GatePolicy(allowed_actions=frozenset({str(method)}), protected_paths=protected_paths)
    if policy.violates_invariant(action):
        problems.append("revised action touches a protected or unreadable path")
    return GuardResult(tuple(problems), diff, swapped, fingerprint)


def reflection_problems(reflection, changes, packet_text):
    problems = []
    evidence = reflection.get("evidence", "") if isinstance(reflection, dict) else ""
    if not isinstance(evidence, str) or len(evidence.strip()) < MIN_EVIDENCE_CHARS:
        problems.append(f"reflection evidence must quote at least {MIN_EVIDENCE_CHARS} characters of the failure")
    elif evidence.strip() not in packet_text:
        problems.append("reflection evidence is not quoted verbatim from the failure packet")
    claimed = reflection.get("changes") if isinstance(reflection, dict) else None
    if not isinstance(claimed, list) or sorted(set(claimed)) != list(changes):
        problems.append(f"reflection claims changes {claimed!r}; the proposal changes {list(changes)!r}")
    return problems


def canonical_copy(value):
    import json
    return json.loads(canonical(value))
