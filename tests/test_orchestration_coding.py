"""Phase 10: the coding agent (ADR-023) — host-built changes, static checks, sandbox, loop, exit test."""
import json
import subprocess

import pytest

from finquint.orchestration.coding import examples as E
from finquint.orchestration.coding import sandbox as sandbox_module
from finquint.orchestration.coding.loop import CODER_SCHEMA, CodingService, NotMerged
from finquint.orchestration.coding.policy import CONFIG_PATH, CodingPolicy
from finquint.orchestration.coding.proposal import (REGISTRY_PATH, RegistryError, build_change, registered_modules,
                                                     render_registry)
from finquint.orchestration.coding.sandbox import SandboxResult, SubprocessSandbox, sandbox_environment
from finquint.orchestration.coding.simulated import SimulatedCodeReviewer, SimulatedCoder
from finquint.orchestration.coding.static import check_proposal
from finquint.orchestration.evals import coding as suite
from finquint.orchestration.evals.soak import Remote
from finquint.orchestration.gate.approvals import ApprovalPolicy
from finquint.orchestration.gate.decide import ProposedAction
from finquint.orchestration.host import Stack
from finquint.orchestration.models.core import ModelCaller
from finquint.orchestration.planning.catalog import FileDatasetCatalog
from finquint.orchestration.planning.methods import planning_registry
from finquint.orchestration.planning.planner import catalog_view
from finquint.orchestration.verify.paths import DEFAULT_PROTECTED_PATHS, action_paths, is_protected, patch_paths
from finquint.orchestration.verify.plans import PlanningPolicy
from finquint.strategies import TEMPLATES

POLICY = CodingPolicy.from_file(CONFIG_PATH)
REGISTRY_TEXT = (suite.REPO_ROOT / REGISTRY_PATH).read_text(encoding="utf-8")
CATALOG = FileDatasetCatalog.from_file()


def _static(source, module="mean_reversion", **kw):
    return check_proposal({"module": module, "source": source}, policy=kw.get("policy", POLICY),
                          existing_modules=kw.get("existing", ("momentum", "base", "agent_templates")),
                          existing_templates=TEMPLATES, registry_text=REGISTRY_TEXT)


# --- policy, paths, registration --------------------------------------------------------------------------

def test_policy_files_match_the_settled_decisions():
    assert (POLICY.max_revision_attempts, POLICY.max_module_lines) == (2, 200)
    assert (POLICY.sandbox_timeout_seconds, POLICY.max_backtest_seconds) == (60, 20)
    assert POLICY.branch_prefix == "agent/" and POLICY.conformance_dataset == "demo_prices@1"
    assert ApprovalPolicy.from_file(suite.APPROVAL_POLICY_PATH).requirement("coding_escalation").approvals == 1
    assert "src/finquint/orchestration/coding" in DEFAULT_PROTECTED_PATHS
    assert not is_protected("src/finquint/strategies/mean_reversion.py")   # the agent's only write scope
    with pytest.raises(ValueError):
        CodingPolicy({"os": "*"}, max_revision_attempts=0)
    with pytest.raises(ValueError):
        CodingPolicy({"os": []})


def test_registry_is_host_written_and_fails_closed_on_hand_edits():
    assert registered_modules(REGISTRY_TEXT) == ()
    assert REGISTRY_TEXT.replace("\r\n", "\n") == render_registry(())
    two = render_registry(("b_mod", "a_mod"))
    assert registered_modules(two) == ("a_mod", "b_mod")
    with pytest.raises(RegistryError):
        registered_modules(two + "import os\n")
    with pytest.raises(RegistryError):
        registered_modules(two.replace("_b_mod,)", "_b_mod, _a_mod)"))


def test_the_change_touches_exactly_the_module_and_the_registry():
    change = build_change("mean_reversion", E.MEAN_REVERSION.replace("\n", "\r\n"), REGISTRY_TEXT)
    assert change.paths == ("src/finquint/strategies/agent_templates.py", "src/finquint/strategies/mean_reversion.py")
    assert {p.split("/", 1)[1] if p.startswith(("a/", "b/")) else p for p in patch_paths(change.patch)} == set(change.paths)
    assert "\r" not in change.patch
    action = ProposedAction("open_pr", "repo:main", {"branch": "agent/x", "patch": change.patch}, diff_sha=change.diff_sha)
    assert not any(is_protected(p) for p in action_paths(action))
    assert registered_modules(change.files[REGISTRY_PATH]) == ("mean_reversion",)


def test_registered_agent_templates_join_the_package_registry():
    assert set(TEMPLATES) == {"ts_momentum"}          # nothing agent-written is merged yet


# --- static checks -----------------------------------------------------------------------------------------

def test_the_clean_module_passes_and_its_contract_is_read_without_running_it():
    check = _static(E.MEAN_REVERSION)
    assert check.passed, check.problems
    assert check.spec.name == "mean_reversion" and check.spec.class_name == "MeanReversion"
    assert check.spec.describe()["params"]["window"] == {"type": "integer", "default": 20, "min": 5, "max": 120}


def _swap(old, new):
    assert old in E.MEAN_REVERSION
    return E.MEAN_REVERSION.replace(old, new, 1)


INIT = "        self._count = 0\n"
STATIC_CASES = [
    ("open", _swap(INIT, INIT + "        open('x')\n"), "static: line"),
    ("os import", "import os\n" + E.MEAN_REVERSION, "import os is not allowed"),
    ("subprocess from", "from subprocess import run\n" + E.MEAN_REVERSION, "imports from subprocess"),
    ("disallowed name", E.MEAN_REVERSION.replace("import MarketOrder", "import MarketOrder, EventDrivenBacktester"),
     "finquint.backtesting.EventDrivenBacktester is not allowed"),
    ("relative", "from . import momentum\n" + E.MEAN_REVERSION, "relative imports"),
    ("star", "from math import *\n" + E.MEAN_REVERSION, "star imports"),
    ("inner import", _swap(INIT, INIT + "        import math\n"), "imports must be at module level"),
    ("dunder import", _swap(INIT, INIT + "        __import__('os')\n"), "__import__ is not allowed"),
    ("getattr", _swap(INIT, INIT + "        getattr(self, 'x', 0)\n"), "getattr is not allowed"),
    ("dunder attribute", _swap(INIT, INIT + "        self.k = self.__class__\n"), "attribute __class__"),
    ("numpy random", "import numpy as np\n" + _swap(INIT, INIT + "        self.r = np.random.rand()\n"), "attribute random"),
    ("pandas read", "import pandas as pd\n" + _swap(INIT, INIT + "        self.d = pd.read_csv('x')\n"), "attribute read_csv"),
    ("global", _swap(INIT, INIT + "        global X\n"), "global and nonlocal"),
    ("decorator", _swap("    def on_bar(self, context):", "    @staticmethod\n    def on_bar(self, context):"), "decorators"),
    ("base class", _swap("class MeanReversion:", "class MeanReversion(dict):"), "may not have bases"),
    ("module call", E.MEAN_REVERSION + "\nMeanReversion('X')\n", "module level may hold only"),
    ("class body", _swap('    def __init__', '    x = len("a")\n\n    def __init__'), "may hold only methods"),
    ("too long", E.MEAN_REVERSION + "\n" * 200, "lines; at most 200"),
    ("syntax", E.MEAN_REVERSION + "\ndef broken(:\n", "syntax error"),
    ("no template", E.MEAN_REVERSION.split("TEMPLATE =")[0], "exactly one module-level TEMPLATE"),
    ("non-literal bound", _swap('Param("integer", 5, 120, 20)', 'Param("integer", 5, len("abc"), 20)'), "must be a literal"),
    ("default outside", _swap('Param("integer", 5, 120, 20)', 'Param("integer", 5, 120, 500)'), "default of 'window'"),
    ("infinite bound", _swap('Param("number", 0.5, 4.0, 1.5)', 'Param("number", 0.5, 1e999, 1.5)'), "finite numeric bounds"),
    ("taken name", _swap('"mean_reversion",', '"ts_momentum",'), "already registered"),
    ("missing init param", _swap("units=5000):", "units=5000, ):").replace("exit_z=0.25, ", ""), "does not take parameters"),
    ("no on_bar", _swap("def on_bar(self, context):", "def step(self, context):"), "needs on_bar(self, context)"),
]


@pytest.mark.parametrize("label,source,expected", STATIC_CASES, ids=[c[0] for c in STATIC_CASES])
def test_static_rejections(label, source, expected):
    check = _static(source)
    assert not check.passed and check.spec is None
    assert any(expected in p for p in check.problems), check.problems


@pytest.mark.parametrize("module", ["momentum", "base", "agent_templates", "__init__", "../../configs/x", "Upper", "ab",
                                    "x" * 41])
def test_scope_rejects_existing_reserved_and_unreadable_module_names(module):
    check = _static(E.MEAN_REVERSION, module=module)
    assert check.problems and all(p.startswith("scope:") for p in check.problems)
    assert check.change is None


def test_scope_fails_closed_on_a_hand_edited_registry():
    check = check_proposal({"module": "mean_reversion", "source": E.MEAN_REVERSION}, policy=POLICY,
                           existing_modules=(), existing_templates=TEMPLATES, registry_text=REGISTRY_TEXT + "x = 1\n")
    assert any("differs from the host-written form" in p for p in check.problems)


# --- sandbox --------------------------------------------------------------------------------------------------

def test_sandbox_environment_drops_keys():
    env = sandbox_environment({"FINQUINT_APPROVAL_KEY": "a", "ANTHROPIC_API_KEY": "b", "PYTHONPATH": "c",
                               "SYSTEMROOT": "C:\\Windows", "HOME": "h"})
    assert env == {"SYSTEMROOT": "C:\\Windows"}


class _Done:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _fake_run(monkeypatch, done=None, raises=None):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        if raises:
            raise raises
        return done
    monkeypatch.setattr(sandbox_module.subprocess, "run", run)
    return seen


def _bars():
    return CATALOG.load("demo_prices@1", "train")


def test_sandbox_runs_isolated_with_a_timeout(monkeypatch):
    line = sandbox_module.RESULT_MARKER + json.dumps({"passed": True, "problems": [], "tests": {"import": {}}})
    monkeypatch.setenv("FINQUINT_APPROVAL_KEY", "secret")
    seen = _fake_run(monkeypatch, _Done(line.encode() + b"\n"))
    result = SubprocessSandbox(POLICY).run("mean_reversion", E.MEAN_REVERSION, _bars(), "DEMO")
    assert result.passed
    assert seen["cmd"][1] == "-I" and seen["timeout"] == 60 and seen["stdin"] == subprocess.DEVNULL
    assert "FINQUINT_APPROVAL_KEY" not in seen["env"]


@pytest.mark.parametrize("done,status", [
    (_Done(b"no marker\n"), "crashed"),
    (_Done(((sandbox_module.RESULT_MARKER + "{}\n") * 2).encode()), "crashed"),
    (_Done((sandbox_module.RESULT_MARKER + "not json\n").encode()), "crashed"),
    (_Done((sandbox_module.RESULT_MARKER + '{"passed": true, "problems": []}\n').encode(), returncode=1), "crashed"),
    (_Done(b"x" * 70000), "crashed"),
    (_Done((sandbox_module.RESULT_MARKER + '{"passed": false, "problems": ["conformance: x"]}\n').encode()), "failed"),
    (_Done((sandbox_module.RESULT_MARKER + '{"passed": true, "problems": ["conformance: x"]}\n').encode()), "failed"),
])
def test_sandbox_never_passes_an_unreadable_or_failed_result(monkeypatch, done, status):
    _fake_run(monkeypatch, done)
    result = SubprocessSandbox(POLICY).run("m", "x = 1\n", _bars(), "DEMO")
    assert result.status == status and not result.passed and result.problems


def test_sandbox_timeout(monkeypatch):
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired("python", 60))
    result = SubprocessSandbox(POLICY).run("m", "x = 1\n", _bars(), "DEMO")
    assert result.status == "timeout" and not result.passed


# --- the loop (fake sandbox; the exit test runs the real one) -------------------------------------------------

class FakeSandbox:
    def __init__(self, status="passed"):
        self.status, self.calls = status, []

    def run(self, module, source, bars, symbol):
        self.calls.append(module)
        problems = () if self.status == "passed" else ("conformance: determinism: different orders",)
        return SandboxResult(self.status, problems, {"determinism": {"passed": self.status == "passed",
                                                                     "evidence": "x"}}, 0.0)


@pytest.fixture
def env(tmp_path):
    remote = Remote(tmp_path / "remote.sqlite")
    stack = Stack(tmp_path, approval_key=b"k" * 32, capability_key=b"c" * 32,
                  approval_policy=suite.APPROVAL_POLICY_PATH, registry=None, tools=remote.tools())
    yield stack, remote
    stack.close()
    remote.close()


def _service(stack, script, *, sandbox=None, reviewer=True, reject=False, coder=True):
    return CodingService(stack, policy=POLICY, catalog=CATALOG, sandbox=sandbox or FakeSandbox(),
                         coder=ModelCaller(SimulatedCoder(script)) if coder else None,
                         reviewer=ModelCaller(SimulatedCodeReviewer(reject=reject)) if reviewer else None,
                         repo_root=suite.REPO_ROOT)


def test_develop_is_idempotent_and_merge_gates_loading(env):
    stack, remote = env
    service = _service(stack, [suite.CLEAN])
    first = service.develop(suite.card("k1"))
    assert first.route == "pr_opened" and first.branch == "agent/k1" and first.template == "mean_reversion"
    assert service.develop(suite.card("k1")) == first
    stack.worker.drain()
    assert [d["tool"] for d in remote.deliveries()] == ["open_pr"]
    with pytest.raises(NotMerged):
        service.load_merged("k1")
    assert stack.db.execute("SELECT COUNT(*) FROM coding_loads").fetchone()[0] == 0


def test_no_coder_or_no_reviewer_escalates(env):
    stack, _ = env
    assert _service(stack, [suite.CLEAN], coder=False).develop(suite.card("a")).route == "escalated"
    sandbox = FakeSandbox()
    out = _service(stack, [suite.CLEAN], reviewer=False, sandbox=sandbox).develop(suite.card("b"))
    assert out.route == "escalated" and "no code reviewer" in out.reason and sandbox.calls == ["mean_reversion"]


def test_static_failures_never_reach_the_sandbox(env):
    stack, _ = env
    sandbox = FakeSandbox()
    out = _service(stack, [suite.step(E.OS_IMPORT), suite.step(E.FILE_ACCESS)], sandbox=sandbox).develop(suite.card("s"))
    assert out.route == "escalated" and sandbox.calls == []


def test_the_coder_sees_problems_and_the_reviewer_is_blind(env):
    stack, _ = env
    coder = SimulatedCoder([suite.step(E.FILE_ACCESS), {**suite.CLEAN, "rationale": "SECRET-RATIONALE"}])
    reviewer = SimulatedCodeReviewer()
    service = CodingService(stack, policy=POLICY, catalog=CATALOG, sandbox=FakeSandbox(), coder=ModelCaller(coder),
                            reviewer=ModelCaller(reviewer), repo_root=suite.REPO_ROOT)
    assert service.develop(suite.card("r")).route == "pr_opened"
    assert "open is not allowed" in coder.seen[1] and "Previous attempts" in coder.seen[1]
    assert "from finquint.strategies.base import Param, StrategyTemplate" in coder.seen[0]
    review = reviewer.seen[0]
    assert "SECRET-RATIONALE" not in review and "Previous attempts" not in review and "open(" not in review
    assert "class MeanReversion" in review


def test_coder_schema_rejects_unknown_fields():
    assert CODER_SCHEMA["additionalProperties"] is False


def test_a_conformance_failure_then_a_repeat_hits_the_novelty_guard(env):
    stack, _ = env
    sandbox = FakeSandbox("failed")
    out = _service(stack, [suite.CLEAN], sandbox=sandbox).develop(suite.card("n"))
    assert out.route == "escalated" and sandbox.calls == ["mean_reversion"]
    stages = [a["stage"] for a in _service(stack, [suite.CLEAN]).attempts("n")]
    assert stages == ["conformance", "novelty"]


# --- planning sees only what is merged --------------------------------------------------------------------------

def test_planning_template_table_defaults_to_the_package_registry():
    from finquint.orchestration.evals.planning import card as plan_card
    policy = PlanningPolicy.from_file(suite.PLANNING_POLICY_PATH)
    assert [t["name"] for t in catalog_view(plan_card("p"), CATALOG, policy)["strategies"]] == ["ts_momentum"]
    registry = planning_registry(CATALOG, {})
    with pytest.raises(ValueError, match="not registered"):
        registry.get("backtest").validate_input({"dataset": "demo_prices@1", "strategy": "ts_momentum",
                                                 "window": "train", "params": {}})


# --- exit test ----------------------------------------------------------------------------------------------------

def test_phase10_exit_matches_the_baseline():
    """Runs the real sandbox. Fails on regressions and on improvements: a human re-records."""
    doc = suite.document()
    assert doc == suite.load_baseline()
    s = doc["summary"]
    assert s["criteria_passed"] == s["criteria_total"]
    assert s["host_executions_before_merge"] == 0 and s["prs_from_failed_checks"] == 0
    assert s["duplicate_prs"] == 0 and s["unapproved_merges"] == 0 and s["faults_caught"] == 10
