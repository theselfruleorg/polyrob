"""066 P1 — the hashed, pinned closure of every lazily installable feature.

One ``<feature>.txt`` per ``core.lazy_deps.LAZY_DEPS`` row, generated from
``requirements.lock`` by ``scripts/gen_lazy_closures.py`` and pinned by
``tests/unit/core/test_lazy_closures_drift.py``. They ship as package data (the
``"*" = ["*.txt"]`` glob), so a PyPI/pipx install carries them too — the lock
itself ships only in the sdist.

067 (one install): the first-party packs ship inside polyrob and their SDKs are
core extras, so a pack has no closure of its own. ``tool.anysite`` is the lazy
row for the discovery pack's one dependency.

A closure is READ from this package directory only. An installer never takes a
path, or any content, from a request: the request names a feature, and the
feature names the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

_DIR = Path(__file__).resolve().parent
#: A feature name: the LAZY_DEPS key shape. Nothing that can walk a path.
FEATURE_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+$")
_HEADER_RE = re.compile(r"^#\s*([a-z-]+):\s*(.+?)\s*$")
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


@dataclass(frozen=True)
class Closure:
    feature: str
    lock_digest: str
    wheel: bool
    text: str                      # the hashed requirements, headers included
    pins: Tuple[Tuple[str, str], ...]   # (canonical name, version), marker forks repeated

    def blocks(self) -> List[Tuple[str, str, str]]:
        """``(canonical name, version, requirement block)`` per entry."""
        out, cur, lines = [], None, []
        for line in self.text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            m = _PIN_RE.match(line)
            if m:
                if cur:
                    out.append((*cur, "\n".join(lines) + "\n"))
                cur, lines = (_canon(m.group(1)), m.group(2)), [line]
            elif cur:
                lines.append(line)
        if cur:
            out.append((*cur, "\n".join(lines) + "\n"))
        return out


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure_dir() -> Path:
    return _DIR


def closure_path(feature: str) -> Optional[Path]:
    """The shipped closure file for *feature*, or None. Never outside this package."""
    if not isinstance(feature, str) or not FEATURE_RE.match(feature):
        return None
    p = (_DIR / f"{feature}.txt").resolve()
    if p.parent != _DIR or not p.is_file():
        return None
    return p


def parse_closure(feature: str, text: str) -> Closure:
    headers = {}
    pins = []
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            headers[m.group(1)] = m.group(2)
            continue
        p = _PIN_RE.match(line)
        if p:
            pins.append((_canon(p.group(1)), p.group(2)))
    if headers.get("feature") != feature:
        raise ValueError(f"closure file for {feature!r} names {headers.get('feature')!r}")
    return Closure(feature=feature, lock_digest=headers.get("lock-digest", ""),
                   wheel=headers.get("wheel", "true").lower() == "true",
                   text=text, pins=tuple(pins))


def read_closure(feature: str) -> Optional[Closure]:
    p = closure_path(feature)
    if p is None:
        return None
    return parse_closure(feature, p.read_text(encoding="utf-8"))


def shipped_lock_digest() -> Optional[str]:
    """The lock digest every shipped closure was generated from (they agree by test)."""
    for p in sorted(_DIR.glob("*.txt")):
        c = parse_closure(p.stem, p.read_text(encoding="utf-8"))
        if c.lock_digest:
            return c.lock_digest
    return None
