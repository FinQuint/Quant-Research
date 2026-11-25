"""Sandbox (ADR-023): where agent-written code runs before a human merges it. Never the host process.

Sandbox is the interface: run(module, source, bars, symbol) -> SandboxResult. SubprocessSandbox is
the first backend: a temporary directory holding only the candidate module and the bars it is
tested on, and a separate `python -I` process (no PYTHON* variables, no user site, script directory
not on the path) whose environment keeps only what the interpreter needs to start, so no approval,
capability or model key reaches it. It has a wall-clock timeout and an output cap, and the directory
is deleted afterwards. The host reads one result line and imports nothing from the sandbox.

This contains honest mistakes (a crash, a hang, a leak through shared state); it is not a security
boundary. The static check is the first line; a container backend can replace this class later.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .conformance import RESULT_MARKER

SRC_ROOT = Path(__file__).resolve().parents[3]
CONFORMANCE_SCRIPT = Path(__file__).with_name("conformance.py")
# Only what CPython needs to start on each platform; everything else is dropped.
KEPT_ENVIRONMENT = ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


@dataclass(frozen=True)
class SandboxResult:
    """status: "passed", "failed" (a conformance test failed), "timeout" or "crashed" (no result line)."""
    status: str
    problems: tuple
    tests: dict
    seconds: float
    output_tail: str = ""

    @property
    def passed(self):
        return self.status == "passed"

    def to_dict(self):
        return {"status": self.status, "problems": list(self.problems), "tests": self.tests,
                "output_tail": self.output_tail}


class Sandbox:
    def run(self, module, source, bars, symbol):
        raise NotImplementedError


def sandbox_environment(environ=None):
    environ = os.environ if environ is None else environ
    return {k: environ[k] for k in KEPT_ENVIRONMENT if k in environ}


class SubprocessSandbox(Sandbox):
    """policy: CodingPolicy (timeout, output cap, runtime limit, lookahead cutoffs)."""

    def __init__(self, policy, *, python=sys.executable, src_root=SRC_ROOT, timeout_seconds=None):
        self.policy, self.python, self.src_root = policy, python, Path(src_root)
        self.timeout = policy.sandbox_timeout_seconds if timeout_seconds is None else timeout_seconds

    def run(self, module, source, bars, symbol):
        with tempfile.TemporaryDirectory(prefix="finquint-sandbox-") as directory:
            work = Path(directory)
            module_file = work / f"{module}.py"
            module_file.write_text(source, encoding="utf-8")
            bars.to_csv(work / "bars.csv", index=False)
            request = {"src": str(self.src_root), "module_file": str(module_file), "module": module,
                       "bars": str(work / "bars.csv"), "symbol": symbol,
                       "max_backtest_seconds": self.policy.max_backtest_seconds,
                       "lookahead_cutoffs": self.policy.lookahead_cutoffs}
            (work / "request.json").write_text(json.dumps(request), encoding="utf-8")
            start = time.perf_counter()
            try:
                done = subprocess.run([self.python, "-I", str(CONFORMANCE_SCRIPT), str(work / "request.json")],
                                      cwd=directory, env=sandbox_environment(), capture_output=True,
                                      timeout=self.timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                seconds = time.perf_counter() - start
                return SandboxResult("timeout", (f"sandbox: no result within {self.timeout} s (killed)",), {},
                                     seconds)
            seconds = time.perf_counter() - start
        cap = self.policy.output_cap_bytes
        out = done.stdout[:cap].decode("utf-8", "replace")
        err = done.stderr[-cap:].decode("utf-8", "replace")
        lines = [line for line in out.splitlines() if line.startswith(RESULT_MARKER)]
        if done.returncode != 0 or len(lines) != 1 or len(done.stdout) > cap:
            why = "output over the cap" if len(done.stdout) > cap else f"exit code {done.returncode}"
            return SandboxResult("crashed", (f"sandbox: no valid result ({why})",), {}, seconds, err[-1000:])
        try:
            result = json.loads(lines[0][len(RESULT_MARKER):])
        except ValueError:
            return SandboxResult("crashed", ("sandbox: unreadable result",), {}, seconds, err[-1000:])
        status = "passed" if result.get("passed") is True and not result.get("problems") else "failed"
        problems = tuple(result.get("problems") or (() if status == "passed" else ("conformance: failed",)))
        return SandboxResult(status, problems, result.get("tests", {}), seconds, err[-1000:])


__all__ = ["CONFORMANCE_SCRIPT", "KEPT_ENVIRONMENT", "SRC_ROOT", "Sandbox", "SandboxResult", "SubprocessSandbox",
           "sandbox_environment"]
