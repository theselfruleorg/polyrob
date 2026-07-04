"""Tests for the sqlite-vec + embedder lines added to doctor_report (Task 12)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from cli.commands.doctor import doctor_report


# ---------------------------------------------------------------------------
# sqlite-vec lines
# ---------------------------------------------------------------------------


def test_doctor_report_has_sqlite_vec_line():
    """doctor_report always includes a sqlite-vec: line."""
    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "sqlite-vec:" in blob


def test_sqlite_vec_loadable_branch(monkeypatch):
    """When _vec_available() is True and vec_connect succeeds → 'loadable'."""

    class _FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider.vec_connect",
        lambda path: _FakeConn(),
    )

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "sqlite-vec: loadable" in blob
    assert "NOT loadable" not in blob


def test_sqlite_vec_not_loadable_branch_unavailable(monkeypatch):
    """When _vec_available() is False → 'NOT loadable' guidance."""
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available",
        lambda: False,
    )

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "NOT loadable" in blob
    assert "apsw + sqlite-vec" in blob


def test_sqlite_vec_not_loadable_branch_connect_fails(monkeypatch):
    """When _vec_available() is True but vec_connect raises → 'NOT loadable'."""

    def _boom(path):
        raise RuntimeError("extension load failed")

    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider.vec_connect",
        _boom,
    )

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "NOT loadable" in blob


def test_sqlite_vec_import_error_does_not_crash(monkeypatch):
    """Even if the entire import of the provider module fails, doctor_report must not raise."""
    import sys

    # Temporarily hide the module so the import inside doctor_report fails.
    saved = sys.modules.get("modules.memory.local_vector_memory_provider")
    sys.modules["modules.memory.local_vector_memory_provider"] = None  # type: ignore[assignment]
    try:
        lines = doctor_report({})
        blob = "\n".join(lines)
        assert "sqlite-vec:" in blob
    finally:
        if saved is None:
            del sys.modules["modules.memory.local_vector_memory_provider"]
        else:
            sys.modules["modules.memory.local_vector_memory_provider"] = saved


# ---------------------------------------------------------------------------
# embedder lines
# ---------------------------------------------------------------------------


def test_doctor_report_has_embedder_line():
    """doctor_report always includes an embedder: line."""
    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "embedder:" in blob


def test_embedder_present_branch(monkeypatch):
    """When sentence_transformers is importable → 'embedder: present'."""
    import types

    fake_spec = importlib.util.spec_from_loader("sentence_transformers", loader=None)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: fake_spec if name == "sentence_transformers" else importlib.util.find_spec.__wrapped__(name),
    )

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "embedder: present" in blob


def test_embedder_absent_branch(monkeypatch):
    """When sentence_transformers is not importable → 'embedder: absent'."""
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None,
    )

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "embedder: absent" in blob


# ---------------------------------------------------------------------------
# T7 — workspace-isolation invariant lines
# ---------------------------------------------------------------------------


def test_doctor_flags_workspace_under_code_root(monkeypatch):
    """Synthetic server env where workspace coincides under code_root → loud line.

    doctor_report stays pure (env dict in, lines out) — we inject a monkeypatched
    resolver rather than a live container so the roots can be made to coincide.
    """
    from core.runtime_paths import RuntimePaths

    code = Path("/fake/code")
    coinciding = RuntimePaths(
        code_root=code,
        config_dir=code / "config",
        data_home=code / "data",
        workspace_root=code / "data" / "task",  # UNDER code_root
    )
    monkeypatch.setattr(
        "cli.commands.doctor.resolve_runtime_paths",
        lambda *, local: coinciding,
    )

    # POLYROB_LOCAL explicitly off → server mode → the invariant is enforced.
    # (Absent now defaults ON to mirror the CLI's build_cli_container, so server-mode
    # tests must set it off explicitly.)
    lines = doctor_report({"POLYROB_LOCAL": "0"})
    blob = "\n".join(lines)
    assert "! WORKSPACE UNDER CODE ROOT — secrets reachable" in blob
    assert "workspace isolation: OK" not in blob


def test_doctor_passes_when_isolated(monkeypatch, tmp_path):
    """POLYROB_DATA_DIR outside the code root (server mode) → 'workspace isolation: OK'."""
    data = tmp_path / "polyrob-data"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data))
    monkeypatch.setenv("POLYROB_LOCAL", "0")  # server mode (absent now defaults ON = local CLI)

    lines = doctor_report(dict(os.environ))
    blob = "\n".join(lines)
    assert "workspace isolation: OK" in blob
    assert "WORKSPACE UNDER CODE ROOT" not in blob
