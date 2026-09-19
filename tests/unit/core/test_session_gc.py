"""056 WS8 — stale per-session directories are collected (dry-run first).

Prod 2026-09-19: `/var/lib/polyrob/sessions/rob/` held 2,118 session dirs (4.0 GB),
1,642 of them untouched for >7 days. The existing GC (a) first runs after 24 h of
uptime — never, with ~28 restarts/day — and (b) only deletes `workspace/`, which on
this deploy is the shared project root and is rightly skipped; the bulk is
`data/`, `feed/`, `logs/`, `screenshots/`. This GC walks the SESSION dirs, never
the project root, keeps anything touched within the TTL, and is DRY-RUN unless
`SESSION_DIR_GC_APPLY=true` — the 2026-08-16/17 rmtree disaster is why.
"""
import os
import time
import uuid

import pytest


def _mk_session(root, user, age_days, size=1024, sid=None):
    sid = sid or str(uuid.uuid4())
    d = root / user / sid
    (d / "data").mkdir(parents=True)
    (d / "feed").mkdir()
    f = d / "data" / "blob.bin"
    f.write_bytes(b"x" * size)
    old = time.time() - age_days * 86400
    for p in [d, d / "data", d / "feed", f]:
        os.utime(p, (old, old))
    return d


def test_dry_run_reports_but_removes_nothing(tmp_path, monkeypatch):
    from core.session_gc import collect_stale_sessions
    monkeypatch.delenv("SESSION_DIR_GC_APPLY", raising=False)
    old = _mk_session(tmp_path, "rob", 30, size=4096)
    fresh = _mk_session(tmp_path, "rob", 1)
    rep = collect_stale_sessions(str(tmp_path), max_age_days=14, apply=False)
    assert rep["candidates"] == 1 and rep["removed"] == 0 and rep["bytes"] >= 4096
    assert old.exists() and fresh.exists()
    assert rep["apply"] is False


def test_apply_removes_only_stale_uuid_dirs(tmp_path):
    from core.session_gc import collect_stale_sessions
    old = _mk_session(tmp_path, "rob", 30)
    fresh = _mk_session(tmp_path, "rob", 1)
    notuuid = tmp_path / "rob" / "project"
    notuuid.mkdir()
    (notuuid / "keep.txt").write_text("x")
    ancient = time.time() - 60 * 86400
    os.utime(notuuid, (ancient, ancient)); os.utime(notuuid / "keep.txt", (ancient, ancient))
    rep = collect_stale_sessions(str(tmp_path), max_age_days=14, apply=True)
    assert rep["removed"] == 1
    assert not old.exists() and fresh.exists() and notuuid.exists(), \
        "a non-session name (the shared project dir) is never a candidate"


def test_a_recently_touched_file_anywhere_keeps_the_session(tmp_path):
    from core.session_gc import collect_stale_sessions
    d = _mk_session(tmp_path, "rob", 30)
    recent = d / "feed" / "new.jsonl"
    recent.write_text("{}")  # fresh mtime deep inside
    rep = collect_stale_sessions(str(tmp_path), max_age_days=14, apply=True)
    assert rep["removed"] == 0 and d.exists()


def test_protected_ids_are_never_candidates(tmp_path):
    from core.session_gc import collect_stale_sessions
    sid = str(uuid.uuid4())
    d = _mk_session(tmp_path, "rob", 30, sid=sid)
    rep = collect_stale_sessions(str(tmp_path), max_age_days=14, apply=True, protect={sid})
    assert rep["removed"] == 0 and d.exists()
