from hashlib import sha256

import pytest

from finquint.research import ExperimentRecord, ResearchStore, hash_artifact


def test_experiment_and_hashed_artifact_registry(tmp_path):
    store = ResearchStore(tmp_path / "registry.sqlite")
    experiment = ExperimentRecord(
        "exp-1", "hyp-1", "abc123", {"rates": "sha256:data"},
        {"lookback": 252}, random_seed=42, environment={"python": "3.12"},
        metrics={"sharpe": 0.8},
    )
    store.register_experiment(experiment)
    path = tmp_path / "result.csv"
    path.write_text("date,pnl\n2026-01-01,1\n", encoding="utf-8")
    artifact = hash_artifact(path, "artifact-1", "exp-1", media_type="text/csv")
    store.register_artifact(artifact)
    assert artifact.sha256 == sha256(path.read_bytes()).hexdigest()
    assert [event["event_type"] for event in store.audit_log()] == [
        "experiment_registered", "artifact_registered",
    ]
    store.close()


def test_artifact_requires_registered_experiment(tmp_path):
    store = ResearchStore(tmp_path / "registry.sqlite")
    path = tmp_path / "result.txt"
    path.write_text("result", encoding="utf-8")
    with pytest.raises(Exception):
        store.register_artifact(hash_artifact(path, "a", "missing"))
    store.close()


def test_artifact_path_must_exist(tmp_path):
    with pytest.raises(ValueError):
        hash_artifact(tmp_path / "missing", "a", "e")
