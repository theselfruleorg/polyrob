"""043 W4 — every console write records who did it, ratcheted.

Before W4 the console mutated state (config, prefs, pending decisions, goal
verbs, cron cancel, invoice settle, pfp, inbox) with no durable actor trail;
only the pause/resume verbs (``core.autonomy_control``) and the app verbs
(``core.app_service.owner_ops``) recorded downstream, both already stamping
``via="webview"``.

The one answer is :func:`webview.audit.console_write`, which stamps
``source="webview"`` + folds ``via="webview"`` into ``attrs``. This module is
the structural proof — mirroring ``test_read_only_gate_ratchet.py``:

- every ``post``/``patch``/``put``/``delete`` route DEFINED in ``webview/``
  either calls ``console_write`` (directly, or via a same-module helper that
  does) OR is on the small allowlist of routes that record an actor DOWNSTREAM
  (apps/pause/resume/halt) or are feed/loopback/auth rails, not owner-console
  mutations of agent state;
- a NEW mutating console route that records nothing fails the ratchet.

Plus the behaviour the ratchet exists to protect: a cron cancel and a goal verb
each write a ``telemetry_events`` row naming the actor + ``via=webview``.
"""
import ast
import pathlib

import pytest

_WEBVIEW = pathlib.Path(__file__).resolve().parents[3] / "webview"

_MUTATING = ("post", "patch", "put", "delete")
_RECORD_NAME = "console_write"

#: Mutating routes that record an actor DOWNSTREAM, or are not owner-console
#: mutations of agent state at all. Each is here for a NAMED reason — not a
#: dumping ground: a new console mutation belongs in the audited set, never
#: here.
_ALLOWLIST = {
    # apps: core.app_service.owner_ops.record stamps user_id + via="webview".
    "api_apps_approve", "api_apps_reject", "api_apps_kill",
    # pause/halt/resume: core.autonomy_control records AUTONOMY_PAUSED/RESUMED.
    "api_pause", "api_halt", "api_resume",
    # feed / loopback / auth rails in server.py + emit_api.py (out of W4 scope,
    # and not owner-console verbs): the agent's telemetry fast-push, the live
    # token stream, the message rail, the sign-in POST (mutates the CALLER's
    # cookie, not console/agent state) and the session-repair rail.
    "internal_emit", "receive_stream_chunk", "send_message_to_session",
    "owner_login_submit", "api_repair",
}


def _py_files():
    return sorted(_WEBVIEW.glob("*.py"))


def _decorator_is_mutating_route(d: ast.AST) -> bool:
    """``@router.post(...)`` / ``@_fastapi.patch(...)`` / ``@_posture_post(...)`` /
    ``@_posture_route("post", ...)`` — the same shapes the read-only ratchet sees."""
    if not isinstance(d, ast.Call):
        return False
    if isinstance(d.func, ast.Attribute) and d.func.attr in _MUTATING:
        return True
    if isinstance(d.func, ast.Name):
        if d.func.id == "_posture_post":
            return True
        if d.func.id == "_posture_route" and d.args:
            first = d.args[0]
            return isinstance(first, ast.Constant) and first.value in _MUTATING
    return False


def _references_console_write(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == _RECORD_NAME:
            return True
    return False


def _recording_functions(tree: ast.AST) -> set:
    """Function names in this module whose body references ``console_write`` —
    so a thin route that delegates to one (inbox's ``_decide``) is audited too."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _references_console_write(node):
                names.add(node.name)
    return names


def _route_is_audited(node: ast.AST, recording: set) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == _RECORD_NAME:
            return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                and sub.func.id in recording:
            return True
    return False


def _mutating_route_handlers():
    """``(file, lineno, name)`` for every mutating route DEFINED in webview/."""
    out = []
    for f in _py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        recording = _recording_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not any(_decorator_is_mutating_route(d) for d in node.decorator_list):
                continue
            out.append((f.name, node.lineno, node.name,
                        _route_is_audited(node, recording)))
    return out


def test_every_mutating_console_route_records_or_is_allowlisted():
    unaudited = [
        f"{fname}:{lineno} {name}"
        for fname, lineno, name, audited in _mutating_route_handlers()
        if not audited and name not in _ALLOWLIST
    ]
    assert unaudited == [], (
        "every mutating console route must call webview.audit.console_write "
        "(directly or via a same-module helper) or be on the downstream-record "
        "allowlist — a new route that records nothing is unauditable")


def test_the_audited_set_is_not_empty():
    """A guard against the ratchet silently passing because nothing matched
    (a refactor that renamed console_write, say)."""
    audited = [n for _, _, n, ok in _mutating_route_handlers() if ok]
    assert len(audited) >= 8, audited


def test_allowlist_names_only_real_routes():
    """Every allowlist entry must name a route that actually exists — a stale
    entry silently exempts a route that no longer records downstream."""
    all_routes = {name for _, _, name, _ in _mutating_route_handlers()}
    stale = sorted(_ALLOWLIST - all_routes)
    assert stale == [], f"allowlist names routes that no longer exist: {stale}"


# --- the helper: fail-open, stamps source + via ----------------------------- #

def test_console_write_stamps_source_and_via(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from core.event_kinds import CONSOLE_CONFIG_WRITE
    from core.event_log import get_event_log
    from webview.audit import console_write

    console_write(CONSOLE_CONFIG_WRITE, user_id="u1", attrs={"key": "x"})
    rows = get_event_log().query(kind=CONSOLE_CONFIG_WRITE)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "u1"
    assert r["source"] == "webview"
    assert r["attrs"]["via"] == "webview"
    assert r["attrs"]["key"] == "x"


def test_console_write_is_fail_open(monkeypatch):
    """A broken telemetry sink must never break the write it observes."""
    import webview.audit as audit

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("core.event_log.get_event_log", _boom)
    # Must not raise.
    audit.console_write("console_config_write", user_id="u1", attrs={"key": "x"})


def test_console_write_respects_the_disable_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "off")
    from core.event_kinds import CONSOLE_CONFIG_WRITE
    from core.event_log import get_event_log
    from webview.audit import console_write

    console_write(CONSOLE_CONFIG_WRITE, user_id="u1", attrs={"key": "x"})
    assert get_event_log().query(kind=CONSOLE_CONFIG_WRITE) == []


# --- behaviour: a cron cancel and a goal verb each name the actor ----------- #

def _client(monkeypatch, tmp_path, user_id="u1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_goal_verb_writes_an_audit_row_naming_the_actor(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from agents.task.goals.board import GoalBoard
    from core.event_kinds import CONSOLE_GOAL_VERB
    from core.event_log import get_event_log

    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id="u1", title="ship the widget")
    client = _client(monkeypatch, tmp_path, user_id="u1")

    r = client.post(f"/api/webgate/goals/{goal.id}/pause")
    assert r.status_code == 200, r.text
    rows = get_event_log().query(kind=CONSOLE_GOAL_VERB)
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["source"] == "webview"
    assert rows[0]["attrs"]["via"] == "webview"
    assert rows[0]["attrs"]["verb"] == "pause"
    assert rows[0]["attrs"]["goal_id"] == goal.id


def test_cron_cancel_writes_an_audit_row_naming_the_actor(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from core.event_kinds import CONSOLE_CRON_CANCEL
    from core.event_log import get_event_log
    from cron.jobs import CronJobStore
    from cron.service import CronService

    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    job = svc.schedule(task="summarise the week", schedule_spec="30m",
                       user_id="u1")
    client = _client(monkeypatch, tmp_path, user_id="u1")

    r = client.post(f"/api/webgate/cron/{job.id}/cancel")
    assert r.status_code == 200, r.text
    rows = get_event_log().query(kind=CONSOLE_CRON_CANCEL)
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["source"] == "webview"
    assert rows[0]["attrs"]["via"] == "webview"
    assert rows[0]["attrs"]["job_id"] == job.id


# --- pfp records the OUTCOME, not the attempt -------------------------------- #
# A no-op / refused pfp op must not write a completed-action row (honest states).

def _pfp_client(monkeypatch, tmp_path, user_id="u1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import webview.pages as pages
    monkeypatch.setattr(pages, "_pfp_owner_required", lambda req: None)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_pfp_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def test_successful_pfp_generate_logs_a_row(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from core.event_kinds import CONSOLE_PFP_WRITE
    from core.event_log import get_event_log

    client, pages = _pfp_client(monkeypatch, tmp_path)
    monkeypatch.setattr(pages, "load_pfp_meta", lambda home, iid: None)
    monkeypatch.setattr("modules.pfp.store.generate_pfp",
                        lambda *a, **k: {"seed": "Rob"})

    r = client.post("/api/pfp/generate")
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = get_event_log().query(kind=CONSOLE_PFP_WRITE)
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["attrs"]["via"] == "webview"
    assert rows[0]["attrs"]["action"] == "generate"
    assert rows[0]["attrs"]["outcome"] == "created"


def test_noop_pfp_generate_logs_no_completed_action_row(monkeypatch, tmp_path):
    """An avatar already exists → the generate is a no-op; nothing changed, so
    no completed-action row (the confident-wrong record this fix closes)."""
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from core.event_kinds import CONSOLE_PFP_WRITE
    from core.event_log import get_event_log

    client, pages = _pfp_client(monkeypatch, tmp_path)
    monkeypatch.setattr(pages, "load_pfp_meta", lambda home, iid: {"seed": "Rob"})

    r = client.post("/api/pfp/generate")
    assert r.status_code == 200 and "already exists" in r.json()["message"]
    assert get_event_log().query(kind=CONSOLE_PFP_WRITE) == []


def test_refused_pfp_keep_logs_no_completed_action_row(monkeypatch, tmp_path):
    """`keep` with no draft raises FileNotFoundError → {ok:false}; the refusal
    must not read as a locked identity in the audit."""
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    from core.event_kinds import CONSOLE_PFP_WRITE
    from core.event_log import get_event_log

    client, _ = _pfp_client(monkeypatch, tmp_path)

    def _no_draft(*a, **k):
        raise FileNotFoundError("no draft")

    monkeypatch.setattr("modules.pfp.store.keep_pfp", _no_draft)
    r = client.post("/api/pfp/keep")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert get_event_log().query(kind=CONSOLE_PFP_WRITE) == []
