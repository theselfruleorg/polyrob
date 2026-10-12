"""One process-wide index of the repository's source, built lazily (W10).

Fifty-odd ratchets and repo-walking tests each re-read and re-parsed the same
~2,000 production files: the top-level ratchet set spent ~50 s of a ~100 s run
parsing one tree over and over. This module reads each file ONCE, parses it
ONCE and walks it ONCE per test process, whatever test asks first.

It only CACHES — it never changes which files a test scans. A test keeps its
own enumeration (``git ls-files``, ``rglob``, a fixed list) and swaps
``ast.parse(path.read_text())`` for ``repo_index().tree(path)``. So a ratchet
scans exactly what it scanned before, only faster.

* :func:`repo_index` — the per-process singleton (also the session fixture
  ``repo_index`` in ``tests/conftest.py``).
* :meth:`RepoIndex.tracked` — ``git ls-files`` once (tuple of POSIX paths).
* :meth:`RepoIndex.text` / :meth:`RepoIndex.tree` / :meth:`RepoIndex.nodes` —
  the file text, its AST, and ``list(ast.walk(tree))``, each cached.

The cached ASTs are shared: treat them as READ-ONLY. A test that needs to
mutate a tree parses its own copy.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

_MISSING = object()


class RepoIndex:
    def __init__(self, root: Path = REPO_ROOT):
        self.root = Path(root)
        self._tracked: Optional[Tuple[str, ...]] = None
        self._text: Dict[Tuple[Path, str, str], object] = {}
        self._tree: Dict[Tuple[Path, str], object] = {}
        self._nodes: Dict[Tuple[Path, str], List[ast.AST]] = {}

    # -- enumeration -------------------------------------------------------
    def tracked(self) -> Tuple[str, ...]:
        """Every git-tracked path (POSIX, repo-relative). Raises if git fails,
        exactly as the per-test ``subprocess.run(..., check=True)`` did."""
        if self._tracked is None:
            out = subprocess.run(["git", "ls-files", "-z"], cwd=self.root,
                                 capture_output=True, text=True, check=True,
                                 timeout=60).stdout
            self._tracked = tuple(p for p in out.split("\0") if p)
        return self._tracked

    def _abs(self, path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    # -- content -----------------------------------------------------------
    def text(self, path, *, encoding: str = "utf-8", errors: str = "strict") -> str:
        """``Path(path).read_text(encoding, errors)``, cached. A decode or OS
        error is cached and re-raised, so a test sees what it saw before."""
        key = (self._abs(path), encoding, errors)
        hit = self._text.get(key, _MISSING)
        if hit is _MISSING:
            try:
                hit = key[0].read_text(encoding=encoding, errors=errors)
            except (OSError, UnicodeDecodeError) as exc:
                hit = exc
            self._text[key] = hit
        if isinstance(hit, BaseException):
            raise hit
        return hit  # type: ignore[return-value]

    def tree(self, path, *, errors: str = "strict") -> ast.Module:
        """``ast.parse`` of the UTF-8 text (decoded with *errors*), cached.
        ``SyntaxError`` (and the read errors of :meth:`text`) are cached and
        re-raised."""
        key = (self._abs(path), errors)
        hit = self._tree.get(key, _MISSING)
        if hit is _MISSING:
            try:
                hit = ast.parse(self.text(key[0], errors=errors), filename=str(key[0]))
            except (SyntaxError, ValueError, OSError, UnicodeDecodeError) as exc:
                hit = exc
            self._tree[key] = hit
        if isinstance(hit, BaseException):
            raise hit
        return hit  # type: ignore[return-value]

    def nodes(self, path, *, errors: str = "strict") -> List[ast.AST]:
        """``list(ast.walk(self.tree(path)))``, cached (same order)."""
        key = (self._abs(path), errors)
        hit = self._nodes.get(key)
        if hit is None:
            hit = list(ast.walk(self.tree(key[0], errors=errors)))
            self._nodes[key] = hit
        return hit


_INDEX: Optional[RepoIndex] = None


def repo_index() -> RepoIndex:
    """The one per-process index (each xdist worker builds its own)."""
    global _INDEX
    if _INDEX is None:
        _INDEX = RepoIndex()
    return _INDEX
