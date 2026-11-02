"""Phase 2 exit tests (protected evaluator) plus path, patch, and invariant behaviour."""
from hashlib import sha256
import subprocess

import pandas as pd
import pytest

from finquint.backtesting import MarketOrder
from finquint.orchestration import (
    DEFAULT_PROTECTED_PATHS, ActionStatus, CapabilityAuthority, CheckResult, GateDecision, GuardedTools,
    PatchError, ProposedAction, ProtectedPathError, SideEffect, SplitManifest, Tool, ToolRegistry,
    check_action_paths, check_frozen_split, check_lookahead, check_point_in_time, decide, default_policy,
    fingerprint, is_protected, judge_improvement, normalize_path, patch_paths,
)
from finquint.orchestration.adapters import ledger_for
from finquint.orchestration.verify import paths as paths_cli
from finquint.research import ResearchStore

PASSED = ActionStatus(CheckResult.PASSED, 0)
SECRET = b"p" * 32


def diff(path, old="x = 1", new="x = 2"):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{old}\n+{new}\n")


TEST_EDIT = diff("tests/test_backtesting.py", "assert result.turnover > 0", "assert True")
SAFE_EDIT = diff("src/finquint/strategies/momentum.py")


def pr(patch, *, paths=None, sha=True):
    params = {"branch": "agent/fix", "patch": patch}
    if paths is not None:
        params["paths"] = paths
    return ProposedAction("open_pr", "repo:main", params, code_sha="abc",
                          diff_sha=sha256(patch.encode()).hexdigest() if sha else None)


# --- exit test 1: a scripted agent attempt to edit tests/ is blocked -------------------------

class ScriptedAgent:
    """Tries to make a failing test pass by editing the test, while declaring a harmless path."""

    def propose(self):
        return pr(TEST_EDIT, paths=["src/finquint/strategies/momentum.py"])


@pytest.fixture
def guarded(tmp_path):
    research = ResearchStore(tmp_path / "research.sqlite")
    applied = []
    tools = ToolRegistry().register(Tool("open_pr", SideEffect.IDEMPOTENT_WRITE,
                                         lambda a, k: applied.append(a.params["patch"]) or {"pr": len(applied)}))
    authority = CapabilityAuthority(SECRET, ttl_seconds=600)
    yield research, GuardedTools(tools, ledger_for(research), authority), authority, applied
    research.close()


def test_exit_scripted_agent_edit_to_tests_is_blocked_by_gate_and_tool(guarded):
    research, tools, authority, applied = guarded
    action = ScriptedAgent().propose()

    assert decide(action, PASSED, default_policy()) is GateDecision.BLOCK
    assert authority.grant(action, GateDecision.BLOCK) is None

    # Even with a valid token (gate bypassed), the tool layer reads the patch and refuses.
    forged = authority.mint("open_pr", fingerprint(action))
    with pytest.raises(ProtectedPathError, match="tests/test_backtesting.py"):
        tools.call("run1", action, token=forged)
    assert applied == []
    refused = [e for e in research.audit_log(subject_id="run1") if e["event_type"] == "tool_call_refused"]
    assert len(refused) == 1 and "protected path" in refused[0]["payload"]["reason"]


def test_safe_patch_passes_gate_and_tool(guarded):
    _, tools, authority, applied = guarded
    action = pr(SAFE_EDIT)
    decision = decide(action, PASSED, default_policy())
    assert decision is GateDecision.AUTO_CONTINUE
    tools.call("run1", action, token=authority.grant(action, decision))
    assert applied == [SAFE_EDIT]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "agent@example.invalid")
    git(root, "config", "user.name", "agent")
    git(root, "config", "core.autocrlf", "false")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
    (root / "src").mkdir()
    (root / "src" / "m.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "agent/fix")
    return root


def test_exit_ci_check_fails_agent_branch_that_edits_tests(repo, capsys):
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git(repo, "commit", "-qam", "make test pass")
    assert paths_cli.main(["--repo", str(repo), "--base", "main", "--fail-on-protected"]) == 1
    assert "tests/test_a.py" in capsys.readouterr().out
    assert paths_cli.main(["--repo", str(repo), "--base", "main"]) == 0  # report-only for humans


def test_ci_check_catches_rename_out_of_tests_and_passes_clean_branch(repo):
    git(repo, "mv", "tests/test_a.py", "src/moved.py")
    git(repo, "commit", "-qm", "hide failing test")
    assert paths_cli.main(["--repo", str(repo), "--base", "main", "--fail-on-protected"]) == 1
    git(repo, "checkout", "-q", "-b", "agent/clean", "main")
    (repo / "src" / "m.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "clean change")
    assert paths_cli.main(["--repo", str(repo), "--base", "main", "--fail-on-protected"]) == 0


def test_ci_check_reports_git_errors(repo):
    assert paths_cli.main(["--repo", str(repo), "--base", "no-such-ref"]) == 2


# --- exit test 2: a leaky backtest fails the invariant check ---------------------------------

def market(n=30):
    closes = [100 + ((i * 7) % 11) - 5 + i * 0.1 for i in range(n)]
    return pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
                         "symbol": "ETF", "open": [c - 0.2 for c in closes], "close": closes})


class Momentum:
    """Honest: uses only context.history."""

    def __init__(self, data=None):
        self.n = 0

    def on_bar(self, context):
        closes = context.history["close"]
        if len(closes) < 3:
            return ()
        target = 10 if closes.iloc[-1] > closes.tail(3).mean() else 0
        qty = target - context.positions.get("ETF", 0)
        if not qty:
            return ()
        self.n += 1
        return (MarketOrder(f"m{self.n}", "ETF", qty, context.timestamp),)


class PeeksAhead(Momentum):
    """Leaky: precomputes tomorrow's return from the data handed to it."""

    def __init__(self, data):
        super().__init__()
        closes = data.set_index("timestamp")["close"]
        self.tomorrow_up = (closes.shift(-1) > closes).to_dict()

    def on_bar(self, context):
        target = 10 if self.tomorrow_up.get(context.timestamp, False) else 0
        qty = target - context.positions.get("ETF", 0)
        if not qty:
            return ()
        self.n += 1
        return (MarketOrder(f"p{self.n}", "ETF", qty, context.timestamp),)


def test_exit_leaky_backtest_fails_lookahead_invariant():
    result = check_lookahead(market(), PeeksAhead)
    assert not result.passed
    assert "future data" in result.evidence
    assert result.details["first_difference_at"]


def test_honest_backtest_passes_lookahead_invariant():
    result = check_lookahead(market(), Momentum, cutoff_count=8)
    assert result.passed and result.details["cutoffs_tested"] == 8


def test_lookahead_fails_closed_on_nondeterminism_and_bad_input():
    import random

    class Coin(Momentum):
        def on_bar(self, context):
            self.n += 1
            return (MarketOrder(f"c{self.n}", "ETF", random.choice([1, -1]), context.timestamp),)

    assert "not deterministic" in check_lookahead(market(), Coin).evidence
    assert not check_lookahead(market().drop(columns="open"), Momentum).passed
    assert not check_lookahead(market(1), Momentum).passed  # nothing to truncate
    assert not check_lookahead(market(), Momentum, cutoffs=["2024-01-01"]).passed


def test_point_in_time_invariant_reuses_10c_check():
    ok = pd.DataFrame({"event_time": ["2025-01-01"], "availability_time": ["2025-01-02"],
                       "decision_time": ["2025-01-02"]})
    leaky = ok.assign(availability_time=["2025-01-03"])
    assert check_point_in_time(ok).passed
    assert not check_point_in_time(leaky).passed


# --- frozen split and acceptance -------------------------------------------------------------

SPLIT = SplitManifest("2020-01-01", "2023-12-31", "2024-01-01", "2024-12-31")


def test_frozen_split_detects_changed_split_and_tuning_on_test_window():
    frozen = SPLIT.digest()
    train = pd.date_range("2020-01-01", "2023-12-31", freq="MS", tz="UTC")
    assert check_frozen_split(SPLIT, frozen_digest=frozen, tuning_timestamps=train).passed

    moved = SplitManifest("2020-01-01", "2024-06-30", "2024-07-01", "2024-12-31")
    assert "changed after it was frozen" in check_frozen_split(moved, frozen_digest=frozen,
                                                               tuning_timestamps=train).evidence

    peek = list(train) + [pd.Timestamp("2024-03-01", tz="UTC")]
    result = check_frozen_split(SPLIT, frozen_digest=frozen, tuning_timestamps=peek)
    assert not result.passed and result.details["in_test_window"] == 1


def test_split_manifest_validation_and_round_trip():
    with pytest.raises(ValueError):
        SplitManifest("2024-01-01", "2024-06-30", "2024-03-01", "2024-12-31")  # overlap
    assert SplitManifest.from_dict(SPLIT.to_dict()).digest() == SPLIT.digest()
    assert SplitManifest("2020-01-01T00:00:00+00:00", "2023-12-31", "2024-01-01", "2024-12-31").digest() == SPLIT.digest()
    assert not check_frozen_split(None, frozen_digest="x", tuning_timestamps=[]).passed


def test_in_sample_only_improvement_is_not_accepted():
    kw = dict(baseline_in_sample=1.0, baseline_out_of_sample=0.5)
    assert judge_improvement(**kw, candidate_in_sample=1.2, candidate_out_of_sample=0.6).passed
    overfit = judge_improvement(**kw, candidate_in_sample=2.0, candidate_out_of_sample=0.4)
    assert not overfit.passed and "in sample but not out of sample" in overfit.evidence
    assert not judge_improvement(**kw, candidate_in_sample=1.2, candidate_out_of_sample=0.55,
                                 min_oos_improvement=0.1).passed
    assert judge_improvement(baseline_in_sample=0.2, baseline_out_of_sample=0.3, candidate_in_sample=0.2,
                             candidate_out_of_sample=0.1, higher_is_better=False).passed  # e.g. drawdown
    assert not judge_improvement(**kw, candidate_in_sample=float("nan"), candidate_out_of_sample=0.6).passed


# --- path normalization and patch parsing ----------------------------------------------------

@pytest.mark.parametrize("path", [
    "tests/x.py", "Tests/x.py", "TESTS\\x.py", "./tests/x.py", "tests./x.py", "tests /x.py",
    "configs/research_policy.yaml", "src/finquint/orchestration/gate/decide.py",
    "src/finquint/orchestration/verify/paths.py", ".github/workflows/tests.yml", "CODEOWNERS",
    "pyproject.toml", "conftest.py",
    "/etc/passwd", "C:/Quant-Research/tests/x.py", "src/../tests/x.py", "tests:stream", "",
])
def test_protected_or_untrusted_paths(path):
    assert is_protected(path)


@pytest.mark.parametrize("path", ["src/finquint/strategies/m.py", "testsuite/x.py", "docs/notes.md",
                                  "src/finquint/orchestration/tools/ledger.py"])
def test_unprotected_paths(path):
    assert not is_protected(path)


def test_normalize_path():
    assert normalize_path("a\\b/./c") == "a/b/c"
    assert normalize_path("a/../b") is None
    assert normalize_path(None) is None


def test_patch_paths_reads_new_deleted_renamed_and_quoted_files():
    new = "diff --git a/src/n.py b/src/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/n.py\n@@ -0,0 +1 @@\n+x\n"
    assert "src/n.py" in patch_paths(new)
    gone = "diff --git a/tests/t.py b/tests/t.py\ndeleted file mode 100644\n--- a/tests/t.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    assert "tests/t.py" in patch_paths(gone)
    rename = ("diff --git a/tests/t.py b/src/t.py\nsimilarity index 100%\n"
              "rename from tests/t.py\nrename to src/t.py\n")
    assert {"tests/t.py", "src/t.py"} <= set(patch_paths(rename))
    quoted = 'diff --git "a/tests/\\303\\251 t.py" "b/tests/\\303\\251 t.py"\nBinary files differ\n'
    assert "tests/\u00e9 t.py" in patch_paths(quoted)
    spaced = "diff --git a/tests/my test.py b/tests/my test.py\nindex 1..2 100644\nBinary files differ\n"
    assert "tests/my test.py" in patch_paths(spaced)


def test_patch_with_removed_line_that_looks_like_a_header_is_read_by_hunk_counts():
    # The removed line "-- tests/x.py" shows as "--- tests/x.py"; it is content, not a header.
    patch = "--- a/src/m.py\n+++ b/src/m.py\n@@ -1,2 +1 @@\n--- tests/x.py\n x\n"
    assert set(patch_paths(patch)) == {"a/src/m.py", "src/m.py", "b/src/m.py"}


def test_second_file_after_a_hunk_is_seen():
    patch = SAFE_EDIT + "--- a/tests/x.py\n+++ b/tests/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert "tests/x.py" in patch_paths(patch)
    assert decide(pr(patch), PASSED, default_policy()) is GateDecision.BLOCK


def test_both_strip_levels_are_checked():
    # Applied with -p0 this edits a/tests/..; with -p1 it edits tests/... Either way it is seen.
    assert not check_action_paths(pr(diff("tests/x.py"))).passed
    no_prefix = "--- tests/x.py\n+++ tests/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert not check_action_paths(pr(no_prefix)).passed


@pytest.mark.parametrize("patch", [
    "@@ -1 +1 @@\n-a\n+b\n",                                    # hunk without file headers
    "--- a/src/m.py\n+++ b/src/m.py\n@@ -1,3 +1,3 @@\n-a\n+b\n",   # truncated hunk
    "--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1,2 @@\n-a\n+b\n",     # hunk shorter on the new side
    "--- a/src/m.py\n+++ b/src/m.py\n@@ bogus @@\n",
    "diff --git a/x b/y z\nindex 1..2\n",                         # unreadable block
    "rm -rf tests\n",
])
def test_malformed_patches_fail_closed(patch):
    with pytest.raises(PatchError):
        patch_paths(patch)
    assert decide(pr(patch), PASSED, default_policy()) is GateDecision.BLOCK


def test_patch_hash_mismatch_and_bad_types_are_blocked():
    action = ProposedAction("open_pr", "repo:main", {"patch": SAFE_EDIT}, code_sha="abc", diff_sha="0" * 64)
    assert decide(action, PASSED, default_policy()) is GateDecision.BLOCK
    assert decide(pr(SAFE_EDIT, sha=False), PASSED, default_policy()) is GateDecision.AUTO_CONTINUE
    bad = ProposedAction("open_pr", "repo:main", {"patch": ["tests/x.py"]}, code_sha="abc")
    assert decide(bad, PASSED, default_policy()) is GateDecision.BLOCK


def test_default_protected_paths_cover_the_spec():
    for root in ("tests", "configs", "src/finquint/evaluation", "src/finquint/backtesting",
                 "src/finquint/orchestration/gate", "src/finquint/orchestration/verify", ".github"):
        assert root in DEFAULT_PROTECTED_PATHS
    assert default_policy().protected_paths == DEFAULT_PROTECTED_PATHS


def test_read_only_tools_are_not_path_checked(tmp_path):
    research = ResearchStore(tmp_path / "r.sqlite")
    tools = GuardedTools(ToolRegistry().register(Tool("research", SideEffect.READ_ONLY, lambda a, k: {"ok": 1})),
                         ledger_for(research), CapabilityAuthority(SECRET, ttl_seconds=600))
    action = ProposedAction("research", "path:tests/test_a.py", {}, code_sha="abc")
    assert tools.call("run1", action).result == {"ok": 1}  # reading tests is fine
    research.close()


def test_status_for_invariants_blocks_on_any_failure():
    from finquint.orchestration.adapters import status_for_invariants

    good = check_lookahead(market(), Momentum)
    bad = check_lookahead(market(), PeeksAhead)
    assert status_for_invariants([good]).checks is CheckResult.PASSED
    status = status_for_invariants([good, bad])
    assert status.checks is CheckResult.FAILED
    assert decide(ProposedAction("run_backtest", "dataset:x", {}), status, default_policy()) is GateDecision.BLOCK
    assert status_for_invariants([]).checks is CheckResult.FAILED  # nothing verified


def test_codeowners_covers_every_protected_path():
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text().splitlines()
    owned = {line.split()[0].strip("/") for line in lines if line.strip() and not line.startswith("#")}
    assert owned == set(DEFAULT_PROTECTED_PATHS)