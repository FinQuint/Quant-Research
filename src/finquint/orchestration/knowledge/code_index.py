"""Code index: exact function and class text per code SHA (spec: "Code: retrieve exact
function text at the current SHA before any edit; summaries never stand in for code").

Standard-library ast only. Each symbol keeps its file, line range and exact source text.
Files are hashed, so a later index can tell which files changed (stale marks).
"""
import ast
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import re

from ..context.evidence import Evidence, words


@dataclass(frozen=True)
class Symbol:
    qualname: str      # module.Class.method
    kind: str          # function | class
    path: str          # repo-relative, forward slashes
    start: int
    end: int
    text: str
    docstring: str = ""

    @property
    def evidence_id(self):
        return "code-" + re.sub(r"[^A-Za-z0-9_-]", "-", self.qualname)


class CodeIndex:
    def __init__(self, sha, symbols, file_hashes):
        self.sha = sha
        self.symbols = {}
        for symbol in symbols:   # same name twice (property getter/setter): later ones get #2, #3
            name, n = symbol.qualname, 1
            while name in self.symbols:
                n += 1
                name = f"{symbol.qualname}#{n}"
            self.symbols[name] = symbol if name == symbol.qualname else replace(symbol, qualname=name)
        self.file_hashes = dict(file_hashes)

    @classmethod
    def build(cls, root, sha, *, package="src/finquint"):
        """Index every .py file under root/package. A file that cannot be decoded or parsed is
        skipped but still hashed, so it counts for staleness. A UTF-8 BOM is allowed."""
        root = Path(root)
        symbols, hashes = [], {}
        for path in sorted((root / package).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            hashes[rel] = sha256(raw).hexdigest()
            try:
                source = raw.decode("utf-8-sig").replace("\r\n", "\n")   # same text on every OS
                tree = ast.parse(source)
            except (UnicodeDecodeError, SyntaxError, ValueError):
                continue   # unreadable: hashed for staleness, not indexed
            module = rel[:-3].replace("/", ".").removeprefix("src.").removesuffix(".__init__")
            symbols += _walk(tree, source, module, rel)
        return cls(sha, symbols, hashes)

    def get(self, qualname):
        return self.symbols.get(qualname)

    def search(self, query, limit=10):
        terms = set(words(query))
        scored = []
        for symbol in self.symbols.values():
            name_terms = set(words(symbol.qualname.replace(".", " ").replace("_", " ")))
            score = 3 * len(terms & name_terms) + len(terms & set(words(symbol.docstring)))
            if score:
                scored.append((score, symbol.qualname))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self.symbols[name] for _, name in scored[:limit]]

    def changed_files(self, previous):
        """Files added, removed or edited since a previous index."""
        old, new = previous.file_hashes, self.file_hashes
        return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))

    def evidence(self):
        """Every symbol as authoritative evidence pinned to this SHA."""
        version = self.sha[:12]
        return [Evidence(s.evidence_id, version, "code", s.text, run_id=None, source=f"file:{s.path}",
                         claims={"path": s.path, "lines": f"{s.start}-{s.end}"})
                for s in sorted(self.symbols.values(), key=lambda s: s.qualname)]


def _walk(tree, source, prefix, path):
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{node.name}"
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            found.append(Symbol(name, kind, path, node.lineno, node.end_lineno,
                                ast.get_source_segment(source, node) or "", ast.get_docstring(node) or ""))
            if isinstance(node, ast.ClassDef):
                found += _walk(node, source, name, path)
    return found
