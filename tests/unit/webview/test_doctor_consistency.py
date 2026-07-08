"""P0-4 (2026-07-06 UX handoff) — the System page must not contradict itself.

The header's ``memory_backend`` field resolved POLYROB_LOCAL with
``bool_env(..., False)`` (absent = off → sqlite) while the ``doctor_report``
checks assumed the CLI context (absent = ON → local_vector), so one page said
both "sqlite" and "local_vector". Both now flow through ONE resolution
(``cli.commands.doctor.local_flag_on`` + ``resolve_memory_backend``), with the
CLI-only "absent means ON" assumption applied only in the CLI: the webview is
a server process (nothing does the ``POLYROB_LOCAL`` setdefault there), so it
resolves with ``absent_means_on=False`` — matching what
``modules.memory.backend_factory`` actually does at runtime.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _doctor_body(monkeypatch, **env):
    for key in ("POLYROB_LOCAL", "ROB_LOCAL", "MEMORY_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    r = TestClient(app).get("/api/webgate/doctor")
    assert r.status_code == 200
    return r.json()


def _checks_backend(body):
    for line in body["checks"]:
        if line.startswith("memory backend: "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError("doctor checks carry no 'memory backend:' line")


def test_header_matches_checks_when_local_unset(monkeypatch):
    body = _doctor_body(monkeypatch)
    assert body["memory_backend"] == _checks_backend(body) == "sqlite"


def test_header_matches_checks_when_local_on(monkeypatch):
    body = _doctor_body(monkeypatch, POLYROB_LOCAL="1")
    assert body["memory_backend"] == _checks_backend(body) == "local_vector"


def test_explicit_memory_backend_wins_everywhere(monkeypatch):
    body = _doctor_body(monkeypatch, MEMORY_BACKEND="none")
    assert body["memory_backend"] == _checks_backend(body) == "none"


def test_cli_doctor_still_assumes_local_on_absent():
    """`polyrob doctor` (CLI context) keeps absent-means-ON — run/chat DO get
    the POLYROB_LOCAL setdefault, so reporting it ON there is honest."""
    from cli.commands.doctor import doctor_report
    lines = doctor_report({})
    assert "memory backend: local_vector" in lines
    assert "POLYROB_LOCAL: ON" in lines
