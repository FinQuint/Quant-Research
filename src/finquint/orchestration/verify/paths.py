"""Protected-path invariant (ADR-005): agents cannot change the evaluator.

The paths an action touches come from the action itself, never only from what the agent
declares. When an action carries a patch, every file the patch names is checked.

Anything unreadable fails closed: absolute or escaping paths, malformed patches, and
patches whose hash does not match the action's diff_sha.

Run as a script to list protected files changed between two git revisions (used by CI):
    python -m finquint.orchestration.verify --base origin/main [--fail-on-protected]
"""
import argparse
from dataclasses import dataclass
from hashlib import sha256
import os
import re
import subprocess
import sys

DEFAULT_PROTECTED_PATHS = (
    # The evaluator: tests, harness, checks, policies, data-split and acceptance configs.
    "tests",
    "configs",
    "src/finquint/evaluation",
    "src/finquint/backtesting",
    # The code that enforces the rules.
    "src/finquint/orchestration/gate",
    "src/finquint/orchestration/verify",
    # Files that change how tests run or who must review.
    ".github",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    "pyproject.toml",
    "conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
)


class PatchError(ValueError):
    """A patch whose file paths cannot be read with certainty."""


class ProtectedPathError(PermissionError):
    """An action touches a protected path; no one can override this (BLOCK)."""


def normalize_path(path):
    """Repo-relative POSIX path, or None if it cannot be trusted."""
    if not isinstance(path, str) or "\0" in path or ":" in path:
        return None  # NUL, drive letters, NTFS alternate streams
    text = path.replace("\\", "/")
    if text.startswith("/"):
        return None  # absolute
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        # Windows ignores trailing dots and spaces: "tests. /x.py" is "tests/x.py".
        part = part.rstrip(". ")
        if not part:
            return None
        parts.append(part)
    return "/".join(parts) or None


def is_protected(path, roots=DEFAULT_PROTECTED_PATHS):
    """True if path is a protected root or lies under one. Unreadable paths count as protected."""
    norm = normalize_path(path)
    if norm is None:
        return True
    key = norm.casefold()  # case-insensitive file systems: "Tests/x.py" writes to tests/
    for root in roots:
        root_key = normalize_path(root)
        if root_key is None:
            raise ValueError(f"invalid protected root: {root!r}")
        root_key = root_key.casefold()
        if key == root_key or key.startswith(root_key + "/"):
            return True
    return False


_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _unquote(text):
    """Git's C-style quoting for unusual file names."""
    if not (len(text) >= 2 and text[0] == '"' and text[-1] == '"'):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt].encode()
            i += 2
        elif re.fullmatch(r"[0-7]{3}", body[i + 1:i + 4]):
            out.append(int(body[i + 1:i + 4], 8))
            i += 4
        else:
            raise PatchError(f"bad escape in quoted path: {text!r}")
    return out.decode("utf-8", "surrogateescape")


def _candidates(raw):
    """Both readings of a header path: as written, and with the first component stripped
    (git apply's default -p1). Checking both means either way of applying is caught."""
    path = _unquote(raw.split("\t", 1)[0].rstrip("\r"))
    if path == "/dev/null":
        return []
    found = [path]
    if "/" in path.lstrip("/"):
        found.append(path.lstrip("/").split("/", 1)[1])
    return found


def _git_header_paths(rest):
    """Paths from 'diff --git a/X b/Y'. Unquoted names may contain spaces, so only the
    unambiguous cases are parsed; renames are covered by their own header lines."""
    if rest.startswith('"'):
        match = re.fullmatch(r'("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*"|\S.*)', rest)
        if not match:
            raise PatchError(f"unreadable diff header: {rest!r}")
        return _candidates(match.group(1)) + _candidates(match.group(2))
    if rest.endswith('"'):
        match = re.fullmatch(r'(\S.*?)\s+("(?:[^"\\]|\\.)*")', rest)
        if not match:
            raise PatchError(f"unreadable diff header: {rest!r}")
        return _candidates(match.group(1)) + _candidates(match.group(2))
    # Same file on both sides: "a/P b/P", so the split point is the middle space.
    half = (len(rest) - 1) // 2
    left, sep, right = rest[:half], rest[half:half + 1], rest[half + 1:]
    if sep == " " and left.split("/", 1)[-1] == right.split("/", 1)[-1]:
        return _candidates(left) + _candidates(right)
    return []


_PAIR_HEADERS = ("rename from ", "rename to ", "copy from ", "copy to ")


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def patch_paths(patch):
    """Every file path a unified diff names, as a sorted tuple. Raises PatchError.

    Hunk bodies are skipped by their line counts, as git apply reads them, so a removed
    line that happens to start with '--- ' is never taken for a file header.
    """
    if not isinstance(patch, str):
        raise PatchError("patch must be text")
    paths, files = set(), 0
    old_left = new_left = 0
    block = None  # paths named by the current "diff --git" block

    def close(block):
        if block is not None and not block:
            raise PatchError("diff block names no readable file")
        paths.update(block or ())

    for line in patch.splitlines():
        line = line.rstrip("\r")
        if old_left or new_left:  # inside a hunk body
            tag = line[:1]
            if tag in (" ", ""):
                old_left, new_left = old_left - 1, new_left - 1
            elif tag == "-":
                old_left -= 1
            elif tag == "+":
                new_left -= 1
            elif tag == "\\":
                continue  # "\ No newline at end of file"
            else:
                raise PatchError("hunk is shorter than its header says")
            if old_left < 0 or new_left < 0:
                raise PatchError("hunk is longer than its header says")
            continue
        if line.startswith("diff --git "):
            close(block)
            files += 1
            block = set(_git_header_paths(line[len("diff --git "):]))
        elif line.startswith(("--- ", "+++ ")):
            files += 1
            found = _candidates(line[4:])
            (block if block is not None else paths).update(found or ["/dev/null"])
        elif line.startswith(_PAIR_HEADERS):
            (block if block is not None else paths).add(_unquote(line.split(" ", 2)[2]))
        elif line.startswith("@@"):
            match = _HUNK.match(line)
            if not match:
                raise PatchError(f"unreadable hunk header: {line!r}")
            old_left = int(match.group(1) or 1)
            new_left = int(match.group(2) or 1)
    if old_left or new_left:
        raise PatchError("patch ends inside a hunk")
    close(block)
    paths.discard("/dev/null")  # creation/deletion marker; the other side names the file
    if not paths:
        if files or patch.strip():
            raise PatchError("patch names no readable file")
    return tuple(sorted(paths))


def action_paths(action):
    """Paths an action touches: a 'path:' target, params["paths"], and every file in
    params["patch"]. Raises PatchError when any of them cannot be read."""
    found = []
    if action.target.startswith("path:"):
        found.append(action.target[len("path:"):])
    listed = action.params.get("paths", [])
    if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
        raise PatchError("params['paths'] must be a list of strings")
    found.extend(listed)
    if "patch" in action.params:
        patch = action.params["patch"]
        if not isinstance(patch, str):
            raise PatchError("params['patch'] must be text")
        if action.diff_sha is not None and action.diff_sha != sha256(patch.encode("utf-8")).hexdigest():
            raise PatchError("diff_sha does not match the patch")
        found.extend(patch_paths(patch))
    return tuple(found)


@dataclass(frozen=True)
class PathCheck:
    passed: bool
    violations: tuple  # (path, reason) pairs
    checked: tuple = ()


def check_action_paths(action, roots=DEFAULT_PROTECTED_PATHS):
    """The protected-path invariant for one action. Fails closed on anything unreadable."""
    try:
        paths = action_paths(action)
    except PatchError as exc:
        return PathCheck(False, (("<action>", str(exc)),))
    return check_paths(paths, roots)


def check_paths(paths, roots=DEFAULT_PROTECTED_PATHS):
    violations = []
    for path in paths:
        if normalize_path(path) is None:
            violations.append((path, "untrusted path"))
        elif is_protected(path, roots):
            violations.append((path, "protected path"))
    return PathCheck(not violations, tuple(violations), tuple(paths))


def changed_paths(base, head="HEAD", *, repo="."):
    """Files changed on head since its merge base with base. Renames count as both names."""
    out = subprocess.run(["git", "-C", repo, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
                         capture_output=True, check=True).stdout
    return tuple(p for p in out.decode("utf-8", "surrogateescape").split("\0") if p)


def main(argv=None):
    parser = argparse.ArgumentParser(description="List protected files changed since a base revision.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--fail-on-protected", action="store_true",
                        help="exit 1 if any protected file changed (used for agent branches)")
    args = parser.parse_args(argv)
    try:
        paths = changed_paths(args.base, args.head, repo=args.repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"could not read changes: {exc}", file=sys.stderr)
        return 2
    result = check_paths(paths)
    lines = [f"Protected files changed: {len(result.violations)} of {len(paths)} changed files"]
    lines += [f"- `{path}` ({reason})" for path, reason in result.violations]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 1 if args.fail_on_protected and result.violations else 0
