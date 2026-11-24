"""The planner's catalog: pinned datasets (DatasetCatalog), strategy templates and planner methods.

DatasetCatalog is the interface the planner and the methods read through. FileDatasetCatalog is the
first backing (protected configs/datasets.json: id@version -> file, SHA-256, frozen split); a
governed data agent (roadmap Phase 11) will register versions behind the same interface.

Every load re-hashes the file and fails closed on a mismatch, so a dataset cannot change under a
pinned reference.
"""
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path
import re

import pandas as pd

from finquint.autonomous import Source
from finquint.backtesting import validate_bars

from ..verify.invariants import SplitManifest

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = REPO_ROOT / "configs" / "datasets.json"
_REF = re.compile(r"^([A-Za-z0-9_-]+)@([A-Za-z0-9_.-]+)$")
WINDOWS = ("train", "test")


class DatasetError(ValueError):
    """Unknown, unpinned or tampered dataset reference."""


def parse_ref(ref):
    match = _REF.match(ref) if isinstance(ref, str) else None
    if match is None:
        raise DatasetError(f"dataset reference must be pinned as id@version, got {ref!r}")
    return match.group(1), match.group(2)


@dataclass(frozen=True)
class DatasetEntry:
    dataset_id: str
    version: str
    path: str                 # repo-relative
    sha256: str
    symbols: tuple
    description: str
    split: SplitManifest

    @property
    def ref(self):
        return f"{self.dataset_id}@{self.version}"

    @property
    def source_id(self):
        return f"dataset-{self.dataset_id}-{self.version}".replace(".", "_")

    def describe(self):
        return {"ref": self.ref, "source_id": self.source_id, "symbols": list(self.symbols),
                "description": self.description, "split": self.split.to_dict(), "sha256": self.sha256}


class DatasetCatalog:
    """Interface: entries(), get(ref), load(ref, window=None) -> validated bars."""

    def entries(self):
        raise NotImplementedError

    def get(self, ref):
        dataset_id, version = parse_ref(ref)
        for entry in self.entries():
            if (entry.dataset_id, entry.version) == (dataset_id, version):
                return entry
        raise DatasetError(f"dataset {ref} is not in the catalog")

    def _read(self, entry):
        raise NotImplementedError

    def load(self, ref, window=None):
        entry = self.get(ref)
        frame = validate_bars(self._read(entry))
        if window is None:
            return frame
        if window not in WINDOWS:
            raise DatasetError(f"window must be one of {WINDOWS}")
        start, end = ((entry.split.train_start, entry.split.train_end) if window == "train"
                      else (entry.split.test_start, entry.split.test_end))
        part = frame[(frame["timestamp"] >= start) & (frame["timestamp"] <= end)].reset_index(drop=True)
        if part.empty:
            raise DatasetError(f"{ref} has no bars in its {window} window")
        return part

    def profile_source(self, ref):
        """The host-written profile the read-only research step reviews (no agent text in it)."""
        entry = self.get(ref)
        frame = self.load(ref)
        split = entry.split.to_dict()
        content = (f"Dataset {entry.ref}: {entry.description} Symbols: {', '.join(entry.symbols)}. "
                   f"{len(frame)} daily bars from {frame['timestamp'].min().date()} to {frame['timestamp'].max().date()}. "
                   f"Frozen split: train {split['train_start'][:10]} to {split['train_end'][:10]}, "
                   f"test {split['test_start'][:10]} to {split['test_end'][:10]}. SHA-256 {entry.sha256}.")
        return Source(entry.source_id, f"dataset:{entry.ref}", entry.version, content)


class FileDatasetCatalog(DatasetCatalog):
    def __init__(self, entries, root=REPO_ROOT):
        self.root = Path(root).resolve()
        self._entries = tuple(entries)
        refs = [e.ref for e in self._entries]
        if len(set(refs)) != len(refs):
            raise DatasetError("duplicate dataset references in the catalog")
        for entry in self._entries:
            self._path(entry)

    def entries(self):
        return self._entries

    def _path(self, entry):
        relative = Path(entry.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DatasetError(f"{entry.ref}: path must stay inside the repository")
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise DatasetError(f"{entry.ref}: path escapes the repository")
        return path

    def _read(self, entry):
        data = self._path(entry).read_bytes()
        digest = sha256(data).hexdigest()
        if digest != entry.sha256:
            raise DatasetError(f"{entry.ref}: content hash {digest[:12]} does not match the catalog")
        frame = pd.read_csv(io.BytesIO(data))
        return frame

    @classmethod
    def from_dict(cls, data, root=REPO_ROOT):
        entries = []
        for item in data["datasets"]:
            parse_ref(f"{item['id']}@{item['version']}")
            entries.append(DatasetEntry(item["id"], str(item["version"]), item["path"], item["sha256"],
                                        tuple(item["symbols"]), item["description"],
                                        SplitManifest.from_dict(item["split"])))
        return cls(entries, root=root)

    @classmethod
    def from_file(cls, path=CONFIG_PATH, root=REPO_ROOT):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), root=root)
