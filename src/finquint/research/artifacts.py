from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    experiment_id: str
    path: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"


def hash_artifact(path, artifact_id: str, experiment_id: str, *, media_type="application/octet-stream") -> ArtifactRecord:
    source = Path(path)
    if not source.is_file():
        raise ValueError("artifact path must identify a file")
    digest = sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return ArtifactRecord(artifact_id, experiment_id, str(source), digest.hexdigest(), source.stat().st_size, media_type)
