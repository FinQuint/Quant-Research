"""Eval and replay harness (roadmap Phase 4): golden tasks, fault suite, replay, baselines.

    python -m finquint.orchestration.evals                 # score the suite against the baseline
    python -m finquint.orchestration.evals --replay        # replay every recorded trace
    python -m finquint.orchestration.evals --write-baseline --record-traces   # humans only

This package is part of the protected evaluator (ADR-005): agents may not change the tasks,
criteria, baselines or traces.
"""
from . import faults, golden
from .harness import (BASELINE_PATH, Comparison, Criterion, Env, GoldenTask, TaskRun, TaskScore, compare,
                      load_baseline, replay_task, run_suite, run_task, save_baseline)
from .replay import TRACES_DIR, Recorder, ReplayMiss, Trace, diff_outcomes, trace_path

SUITE = golden.TASKS + faults.TASKS


def task(name):
    for item in SUITE:
        if item.name == name:
            return item
    raise KeyError(f"no golden task named {name}")


__all__ = ["BASELINE_PATH", "Comparison", "Criterion", "Env", "GoldenTask", "Recorder", "ReplayMiss", "SUITE",
           "TRACES_DIR", "TaskRun", "TaskScore", "Trace", "compare", "diff_outcomes", "load_baseline",
           "replay_task", "run_suite", "run_task", "save_baseline", "task", "trace_path"]
