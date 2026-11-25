"""Checks before execution (ADR-023): scope, static (AST) and the template contract.

Nothing here imports or runs the agent's code: the module is only parsed. Any problem rejects the
attempt. check_proposal(proposal, ...) -> StaticCheck(problems, spec, change).

  scope     a valid, unused module name; the host-built patch touches exactly the new module and the
            registration file; no protected path (ADR-012)
  static    imports only from the policy allowlist, at module level; no builtins that reach the
            file system, processes, the interpreter or other objects' internals; no dunder attributes
            except __init__; no I/O-shaped attributes (read_*, to_csv, load, save, ...); no global or
            nonlocal; module and class bodies hold only definitions and literal constants; size limit
  contract  exactly one TEMPLATE = StrategyTemplate("name", "description", {"p": Param(...)}, Class)
            with literal arguments, finite bounds and defaults within them; Class is defined in the
            module, has on_bar(self, context), and its __init__ takes symbol plus every parameter
"""
import ast
from dataclasses import dataclass
from math import isfinite
import re

from finquint.strategies.base import PARAM_KINDS, Param

from ..verify.paths import PatchError, check_paths, patch_paths
from .proposal import MODULE_NAME, RESERVED_MODULES, RegistryError, build_change

TEMPLATE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "print", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "hasattr", "breakpoint", "exit", "quit", "help", "memoryview", "type",
    "id", "object", "classmethod", "staticmethod", "property", "super"})
SAFE_DUNDER_ATTRIBUTES = frozenset({"__init__"})
BANNED_ATTRIBUTES = frozenset({
    "load", "loads", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "fromfile",
    "tofile", "memmap", "ctypeslib", "random", "system", "popen", "lib", "testing", "f2py", "distutils",
    "eval", "query", "pipe", "style", "plot", "io", "api"})
IO_ATTRIBUTE = re.compile(r"^(read_|to_(csv|pickle|parquet|json|excel|sql|hdf|feather|html|clipboard|stata|latex|"
                          r"markdown|xml|gbq|orc|string)$)")
MAX_DESCRIPTION = 400


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    description: str
    params: dict          # name -> Param
    class_name: str

    def describe(self):
        return {"name": self.name, "description": self.description,
                "params": {k: v.to_dict() for k, v in sorted(self.params.items())}}


@dataclass(frozen=True)
class StaticCheck:
    problems: tuple
    spec: TemplateSpec = None
    change: object = None          # proposal.Change when the scope check could build it

    @property
    def passed(self):
        return not self.problems


# --- scope ----------------------------------------------------------------------------------------

def _scope(module, source, existing_modules, registry_text, problems):
    if not isinstance(module, str) or not MODULE_NAME.match(module):
        problems.append(f"scope: module name {module!r} must be lowercase letters, digits and underscores "
                        "(3-40 characters, starting with a letter)")
        return None
    if module in RESERVED_MODULES or module in set(existing_modules):
        problems.append(f"scope: module {module!r} already exists; write a new module")
        return None
    if not isinstance(source, str) or not source.strip():
        problems.append("scope: source must be the whole module text")
        return None
    try:
        change = build_change(module, source, registry_text)
        paths = patch_paths(change.patch)
    except (RegistryError, PatchError) as exc:
        problems.append(f"scope: {exc}")
        return None
    readings = {p[2:] if p.startswith(("a/", "b/")) else p for p in paths}
    if readings != set(change.paths):
        problems.append(f"scope: the change touches {sorted(readings)}, not only {sorted(change.paths)}")
    check = check_paths(paths)
    for path, why in check.violations:
        problems.append(f"scope: {why}: {path}")
    return change


# --- static -----------------------------------------------------------------------------------------

def _literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _imports(tree, allowed, problems):
    top = set(map(id, tree.body))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if id(node) not in top:
            problems.append(f"static: line {node.lineno}: imports must be at module level")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if allowed.get(alias.name) != "*":
                    problems.append(f"static: line {node.lineno}: import {alias.name} is not allowed")
            continue
        if node.level or node.module is None:
            problems.append(f"static: line {node.lineno}: relative imports are not allowed")
            continue
        names = allowed.get(node.module)
        if names is None and node.module != "__future__":
            problems.append(f"static: line {node.lineno}: imports from {node.module} are not allowed")
            continue
        for alias in node.names:
            if alias.name == "*":
                problems.append(f"static: line {node.lineno}: star imports are not allowed")
            elif names not in ("*", None) and alias.name not in names:
                problems.append(f"static: line {node.lineno}: {node.module}.{alias.name} is not allowed")


def _nodes(tree, problems):
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                problems.append(f"static: line {line}: {node.id} is not allowed")
            elif node.id.startswith("__") and node.id.endswith("__"):
                problems.append(f"static: line {line}: {node.id} is not allowed")
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__") and attr not in SAFE_DUNDER_ATTRIBUTES:
                problems.append(f"static: line {line}: attribute {attr} is not allowed")
            elif attr in BANNED_ATTRIBUTES or IO_ATTRIBUTE.match(attr):
                problems.append(f"static: line {line}: attribute {attr} (I/O, randomness or reflection) is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"static: line {line}: global and nonlocal are not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            problems.append(f"static: line {line}: async code is not allowed")
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:
            problems.append(f"static: line {line}: decorators are not allowed")
        elif isinstance(node, ast.ClassDef) and (node.bases or node.keywords):
            problems.append(f"static: line {line}: class {node.name} may not have bases or a metaclass")


def _bodies(tree, problems):
    for index, node in enumerate(tree.body):
        if index == 0 and _is_docstring(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.ClassDef):
            for inner_index, inner in enumerate(node.body):
                if inner_index == 0 and _is_docstring(inner):
                    continue
                if isinstance(inner, ast.FunctionDef):
                    continue
                if isinstance(inner, ast.Assign) and _literal(inner.value) and all(
                        isinstance(t, ast.Name) for t in inner.targets):
                    continue
                problems.append(f"static: line {inner.lineno}: class {node.name} may hold only methods and "
                                "literal constants")
            continue
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            if [t.id for t in node.targets] == ["TEMPLATE"] or _literal(node.value):
                continue
        problems.append(f"static: line {node.lineno}: module level may hold only imports, definitions and "
                        "literal constants")


# --- contract ---------------------------------------------------------------------------------------

def _value(node, what, problems):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        problems.append(f"contract: {what} must be a literal")
        return None


def _call_args(call, names, what, problems):
    """Positional and keyword arguments of a call, mapped onto names. None on a problem."""
    if len(call.args) > len(names) or any(k.arg is None for k in call.keywords):
        problems.append(f"contract: {what} takes {', '.join(names)}")
        return None
    out = dict(zip(names, call.args))
    for keyword in call.keywords:
        if keyword.arg not in names or keyword.arg in out:
            problems.append(f"contract: {what} has an unexpected or repeated argument {keyword.arg!r}")
            return None
        out[keyword.arg] = keyword.value
    return out


def _param(name, node, problems):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Param"):
        problems.append(f"contract: parameter {name!r} must be Param(...)")
        return None
    args = _call_args(node, ("kind", "minimum", "maximum", "default"), f"Param for {name!r}", problems)
    if args is None:
        return None
    values = {k: _value(v, f"Param {name!r} {k}", problems) for k, v in args.items()}
    kind = values.get("kind")
    if kind not in PARAM_KINDS:
        problems.append(f"contract: parameter {name!r} kind must be one of {PARAM_KINDS}")
        return None
    param = Param(kind, values.get("minimum"), values.get("maximum"), values.get("default"))
    if kind != "boolean":
        bounds = (param.minimum, param.maximum)
        if any(isinstance(b, bool) or not isinstance(b, (int, float)) or not isfinite(b) for b in bounds):
            problems.append(f"contract: parameter {name!r} needs finite numeric bounds")
            return None
        if kind == "integer" and not all(isinstance(b, int) for b in bounds):
            problems.append(f"contract: integer parameter {name!r} needs integer bounds")
            return None
        if param.minimum > param.maximum:
            problems.append(f"contract: parameter {name!r} has minimum above maximum")
            return None
    try:
        param.check(name, param.default)
    except (TypeError, ValueError) as exc:
        problems.append(f"contract: default of {name!r}: {exc}")
        return None
    return param


def _function_args(function):
    a = function.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _contract(tree, existing_templates, problems):
    assigns = [n for n in tree.body if isinstance(n, ast.Assign) and [getattr(t, "id", None) for t in n.targets] == ["TEMPLATE"]]
    if len(assigns) != 1:
        problems.append("contract: define exactly one module-level TEMPLATE = StrategyTemplate(...)")
        return None
    call = assigns[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "StrategyTemplate"):
        problems.append("contract: TEMPLATE must be StrategyTemplate(name, description, params, factory)")
        return None
    args = _call_args(call, ("name", "description", "params", "factory"), "StrategyTemplate", problems)
    if args is None:
        return None
    if set(args) != {"name", "description", "params", "factory"}:
        problems.append("contract: StrategyTemplate needs name, description, params and factory")
        return None
    name = _value(args["name"], "template name", problems)
    description = _value(args["description"], "template description", problems)
    if not isinstance(name, str) or not TEMPLATE_NAME.match(name):
        problems.append("contract: template name must be lowercase letters, digits and underscores (3-40)")
    elif name in set(existing_templates):
        problems.append(f"contract: template name {name!r} is already registered")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        problems.append(f"contract: template description must be nonempty text of at most {MAX_DESCRIPTION} characters")
    params_node = args["params"]
    if not isinstance(params_node, ast.Dict) or not params_node.keys:
        problems.append("contract: params must be a nonempty literal dict of Param(...)")
        return None
    params = {}
    for key, value in zip(params_node.keys, params_node.values):
        pname = _value(key, "parameter name", problems) if key is not None else None
        if not isinstance(pname, str) or not pname.isidentifier() or pname == "symbol" or pname in params:
            problems.append(f"contract: parameter name {pname!r} must be a unique identifier other than symbol")
            continue
        param = _param(pname, value, problems)
        if param is not None:
            params[pname] = param
    factory = args["factory"]
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    if not isinstance(factory, ast.Name) or factory.id not in classes:
        problems.append("contract: factory must be a class defined in this module")
        return None
    cls = classes[factory.id]
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    on_bar = methods.get("on_bar")
    if on_bar is None or _function_args(on_bar)[:2] != ["self", "context"] or len(_function_args(on_bar)) != 2:
        problems.append(f"contract: class {cls.name} needs on_bar(self, context)")
    init = methods.get("__init__")
    init_args = _function_args(init) if init is not None else []
    if init_args[:2] != ["self", "symbol"]:
        problems.append(f"contract: {cls.name}.__init__ must take (self, symbol, ...)")
    missing = sorted(set(params) - set(init_args))
    if missing:
        problems.append(f"contract: {cls.name}.__init__ does not take parameters {missing}")
    if problems:
        return None
    return TemplateSpec(name, description, params, cls.name)


# --- entry point ------------------------------------------------------------------------------------

def check_proposal(proposal, *, policy, existing_modules, existing_templates, registry_text):
    """proposal: {"module": ..., "source": ...}. existing_modules: module names already in the
    strategies package; existing_templates: registered template names; registry_text: the current
    agent_templates.py."""
    problems = []
    module, source = proposal.get("module"), proposal.get("source")
    change = _scope(module, source, existing_modules, registry_text, problems)
    if change is None:
        return StaticCheck(tuple(problems))
    lines = source.count("\n") + 1
    if lines > policy.max_module_lines:
        problems.append(f"static: {lines} lines; at most {policy.max_module_lines}")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        problems.append(f"static: syntax error at line {exc.lineno}: {exc.msg}")
        return StaticCheck(tuple(problems), change=change)
    _imports(tree, policy.allowed_imports, problems)
    _nodes(tree, problems)
    _bodies(tree, problems)
    contract_problems = []
    spec = _contract(tree, existing_templates, contract_problems)
    problems.extend(contract_problems)
    return StaticCheck(tuple(dict.fromkeys(problems)), spec if not problems else None, change)


__all__ = ["BANNED_ATTRIBUTES", "BANNED_NAMES", "StaticCheck", "TemplateSpec", "check_proposal"]
