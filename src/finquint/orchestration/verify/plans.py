"""Plan checks (ADR-022): deterministic verification of a planner's proposal before anything runs.

check_plan(proposal, card=..., catalog=..., registry=..., policy=...) -> PlanCheck. Any problem
rejects the plan. The checks, in the order the spec lists them:

  allowlist and validators  every method is a planner method and its input validator accepts
  pinned datasets           every dataset is id@version, on the card, in the catalog, hash intact
  split discipline          parameter_search reads train only; no search depends on a test-window call;
                            params_from / source_task name a direct dependency of the right method
  coverage                  every card criterion is served; every call serves a card criterion
  size and budget           at most max_plan_calls calls; estimated cost within the card's budget
  graph and roles           unique IDs, known dependencies, acyclic, research + execution (Planner)
  protected paths           no parameter names a path
A plan that passes becomes an ordinary autonomous Request (built here, so nothing else parses it).
"""
from dataclasses import dataclass, field
import json
from types import MappingProxyType

from finquint.autonomous import Call, Planner, Request
from finquint.autonomous.models import canonical, identifier

from ..gate.decide import GatePolicy, ProposedAction
from .paths import DEFAULT_PROTECTED_PATHS


@dataclass(frozen=True)
class PlanningPolicy:
    planner_methods: frozenset
    max_plan_calls: int = 12
    max_plan_attempts: int = 2
    method_costs: dict = field(default_factory=dict)
    default_plan_budget: float = 60
    template_threshold: int = 3
    plan_templates: tuple = ()
    version: str = "1"

    def __post_init__(self):
        object.__setattr__(self, "planner_methods", frozenset(self.planner_methods))
        for name in ("max_plan_calls", "max_plan_attempts", "template_threshold"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        costs = dict(self.method_costs)
        if set(costs) != set(self.planner_methods):
            raise ValueError("method_costs must price exactly the planner methods")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in costs.values()):
            raise ValueError("method costs must be non-negative numbers")
        if isinstance(self.default_plan_budget, bool) or not self.default_plan_budget > 0:
            raise ValueError("default_plan_budget must be positive")
        templates = tuple(self.plan_templates)
        for item in templates:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("calls"), list):
                raise ValueError("a plan template needs a key and calls")
        if len({t["key"] for t in templates}) != len(templates):
            raise ValueError("plan template keys must be unique")
        object.__setattr__(self, "method_costs", MappingProxyType(costs))
        object.__setattr__(self, "plan_templates", templates)

    def template(self, key):
        return next((t for t in self.plan_templates if t["key"] == key), None) if key else None

    @classmethod
    def from_dict(cls, data):
        return cls(frozenset(data["planner_methods"]), data.get("max_plan_calls", 12), data.get("max_plan_attempts", 2),
                   data.get("method_costs", {}), data.get("default_plan_budget", 60), data.get("template_threshold", 3),
                   tuple(data.get("plan_templates", ())), str(data.get("version", "1")))

    @classmethod
    def from_file(cls, path):
        from pathlib import Path
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class PlanCheck:
    problems: tuple
    request: object = None   # the Request when there are no problems
    cost: float = 0.0

    @property
    def passed(self):
        return not self.problems


def plan_cost(calls, policy):
    total = 0.0
    for call in calls:
        unit = policy.method_costs.get(call.get("method"), 0)
        grid = call.get("parameters", {}).get("grid") if call.get("method") == "parameter_search" else None
        combos = 1
        if isinstance(grid, dict):
            for values in grid.values():
                combos *= len(values) if isinstance(values, list) else 1
        total += unit * combos
    return total


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _ancestors(task_id, deps):
    seen, stack = set(), list(deps.get(task_id, ()))
    while stack:
        item = stack.pop()
        if item not in seen and item in deps:
            seen.add(item)
            stack.extend(deps[item])
    return seen


def check_plan(proposal, *, card, catalog, registry, policy, run_id=None, code_version=None):
    problems = []
    calls = proposal.get("calls") if isinstance(proposal, dict) else None
    if not isinstance(calls, list) or not calls:
        return PlanCheck(("the plan has no calls",))
    if len(calls) > policy.max_plan_calls:
        problems.append(f"the plan has {len(calls)} calls; at most {policy.max_plan_calls}")
    ids, deps, by_id = [], {}, {}
    for index, call in enumerate(calls):
        where = f"call {index + 1}"
        if not isinstance(call, dict):
            problems.append(f"{where} is not an object")
            continue
        task_id = call.get("task_id")
        try:
            identifier(task_id)
        except ValueError:
            problems.append(f"{where}: task_id must be letters, digits, _ or -")
            continue
        where = f"call {task_id}"
        ids.append(task_id)
        by_id[task_id] = call
        dependencies = call.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
            problems.append(f"{where}: dependencies must be a list of task IDs")
            dependencies = []
        deps[task_id] = list(dependencies)
        method, params = call.get("method"), call.get("parameters")
        if method not in policy.planner_methods:
            problems.append(f"{where}: method {method!r} is not a planner method")
            continue
        if not isinstance(params, dict):
            problems.append(f"{where}: parameters must be an object")
            continue
        try:
            registry.get(method).validate_input(json.loads(canonical(params)))
        except Exception as exc:
            problems.append(f"{where}: {method} rejects its parameters: {exc}")
        for text in _strings(params):
            if "/" in text or "\\" in text:
                problems.append(f"{where}: parameter value {text!r} looks like a path; plans never name files")
        if GatePolicy(allowed_actions=frozenset({method}), protected_paths=DEFAULT_PROTECTED_PATHS).violates_invariant(
                ProposedAction(method, f"plan:{task_id}", params)):
            problems.append(f"{where}: touches a protected or unreadable path")
        if "dataset" in params:
            ref = params["dataset"]
            try:
                entry = catalog.get(ref)
                pinned = dict(card.dataset_versions).get(entry.dataset_id)
                if pinned is None:
                    problems.append(f"{where}: dataset {ref} is not pinned on the task card")
                elif pinned != entry.version:
                    problems.append(f"{where}: dataset {ref} differs from the card's pin {entry.dataset_id}@{pinned}")
                else:
                    catalog.load(ref)   # re-hashes the file: a tampered dataset fails here
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
        criteria = call.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            problems.append(f"{where}: serves no acceptance criterion")
        else:
            unknown = sorted(set(map(str, criteria)) - set(card.criterion_ids))
            if unknown:
                problems.append(f"{where}: serves unknown criteria {unknown}")
    if len(set(ids)) != len(ids):
        problems.append("task IDs must be unique")
    for task_id, call in by_id.items():
        for dep in deps[task_id]:
            if dep not in by_id:
                problems.append(f"call {task_id}: depends on unknown task {dep!r}")
        params = call.get("parameters") if isinstance(call.get("parameters"), dict) else {}
        for key, needed in (("params_from", "parameter_search"), ("source_task", "backtest")):
            if key in params:
                ref = params[key]
                if ref not in deps[task_id]:
                    problems.append(f"call {task_id}: {key} {ref!r} must be a direct dependency")
                elif by_id.get(ref, {}).get("method") != needed:
                    problems.append(f"call {task_id}: {key} {ref!r} must be a {needed} call")
    # Split discipline: tuning never reads, or follows from, the test window.
    for task_id, call in by_id.items():
        if call.get("method") != "parameter_search":
            continue
        if (call.get("parameters") or {}).get("window") != "train":
            problems.append(f"call {task_id}: parameter_search must use the train window")
        for ancestor in _ancestors(task_id, deps):
            if (by_id[ancestor].get("parameters") or {}).get("window") == "test":
                problems.append(f"call {task_id}: tunes after test-window call {ancestor} (test data would leak into tuning)")
    profiles = set()
    for call in by_id.values():
        ref = (call.get("parameters") or {}).get("dataset")
        try:
            profiles.add(catalog.get(ref).source_id) if ref else None
        except ValueError:
            pass
    for task_id, call in by_id.items():
        if call.get("method") == "source_review":
            wanted = (call.get("parameters") or {}).get("source_ids") or []
            stray = sorted(set(map(str, wanted)) - profiles)
            if stray:
                problems.append(f"call {task_id}: reviews {stray}, which are not profiles of datasets the plan uses "
                                f"({sorted(profiles)})")
    served = {str(c) for call in by_id.values() for c in (call.get("criteria") or []) if isinstance(call.get("criteria"), list)}
    missing = sorted(set(card.criterion_ids) - served)
    if missing:
        problems.append(f"no call serves criteria {missing}")
    cost = plan_cost(list(by_id.values()), policy)
    budget = dict(card.budget_limits).get("plan_cost", policy.default_plan_budget)
    if cost > float(budget):
        problems.append(f"estimated cost {cost:g} exceeds the budget {budget}")
    if problems:
        return PlanCheck(tuple(dict.fromkeys(problems)), None, cost)
    try:
        request = build_request(proposal, card=card, catalog=catalog, run_id=run_id, code_version=code_version)
        Planner().build(request, registry)   # graph, cycles, roles: the existing rules, unchanged
    except Exception as exc:
        return PlanCheck((f"plan rejected by the planner rules: {exc}",), None, cost)
    return PlanCheck((), request, cost)


def build_request(proposal, *, card, catalog, run_id=None, code_version=None, max_attempts=2):
    calls = tuple(Call(c["task_id"], c["method"], json.loads(canonical(c["parameters"])), tuple(c.get("dependencies", ())))
                  for c in proposal["calls"])
    refs = sorted({c["parameters"]["dataset"] for c in proposal["calls"] if "dataset" in c["parameters"]})
    sources = tuple(catalog.profile_source(ref) for ref in refs)
    return Request(run_id or card.run_id, card.goal, code_version or card.code_sha, calls, sources, max_attempts)
