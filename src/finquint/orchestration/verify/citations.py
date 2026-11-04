"""Citation checks: every cited evidence ID, version, file and line must exist (spec: verify/citations.py).

A citation fails when the evidence is unknown, superseded by a newer version, deprecated,
provisional (spec: "never cited as fact"), from another run, or (when `offered` is given) was never shown to the model. A file citation
fails when the file is not in the snapshot at the task's SHA or the line is out of range.
"""


def check_citation(evidence_id, version, *, store, run_id, offered=None):
    """None if the citation holds, else the reason it fails."""
    item = store.get(evidence_id, version)
    if item is None:
        return f"{evidence_id} v{version}: no such evidence"
    current = store.current(evidence_id)
    if current is not None and current.ref != item.ref:
        return f"{evidence_id} v{version}: superseded by v{current.version}"
    if item.deprecated:
        return f"{evidence_id} v{version}: deprecated"
    if item.tier == "provisional":
        return f"{evidence_id} v{version}: provisional, never cited as fact"
    if item.run_id is not None and item.run_id != run_id:
        return f"{evidence_id} v{version}: belongs to run {item.run_id}"
    if offered is not None and (evidence_id, version) not in offered:
        return f"{evidence_id} v{version}: not in the evidence the model was given"
    return None


def check_citations(citations, *, store, run_id, offered=None):
    """citations: iterable of {"evidence_id", "version"}. Returns a list of failure reasons."""
    failures = []
    for citation in citations:
        try:
            evidence_id, version = citation["evidence_id"], citation["version"]
        except (KeyError, TypeError):
            failures.append(f"malformed citation {citation!r}")
            continue
        reason = check_citation(evidence_id, version, store=store, run_id=run_id, offered=offered)
        if reason:
            failures.append(reason)
    return failures


def check_file_reference(path, line, *, files):
    """files: {path: text} at the task's code SHA. None if path and line exist."""
    if path not in files:
        return f"{path}: not in the source snapshot"
    count = len(files[path].splitlines())
    if type(line) is not int or not 1 <= line <= count:
        return f"{path}:{line}: line out of range (file has {count} lines)"
    return None
