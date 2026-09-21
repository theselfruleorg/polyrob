"""The 2026-09-21 interface audit, section A — the console's Python half.

One module for the findings that had no home in an existing test file. Each
test names the defect it pins, because every one of them is the same shape: a
surface that answered confidently over something it had not read, or a control
that named a seat which does not exist.

Covered here: A5 (goal ids resolved by id, not inside a claim-queue window),
A7 (a goal carries its session so Work › Now can stop drawing one run twice),
A8/E2 (`/api/webgate/pending` composes through the Inbox and names a refusing
store), A17 (a slash verb on cold open is a command, not a task), A27 (an ask
can be ANSWERED, not only approved), A33 (the money reader uses shared seams),
A35 (the outstanding total is over every pending row, not over one page) and
A44 (the console can size a goal's run).
"""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# --- helpers ----------------------------------------------------------------- #

def _pages_client(monkeypatch, tmp_path, user_id="alice"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages.webgate, "data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def _board(tmp_path):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(os.path.join(str(tmp_path), "goals.db"))


# --- A5: a goal id is resolved by id ----------------------------------------- #

def test_a_goal_verb_resolves_past_the_old_thousand_row_window(monkeypatch,
                                                               tmp_path):
    """⚠️ The verb used to scan ``board.list(limit=1000)`` — the dispatcher's
    priority-ordered CLAIM queue, forbidden as a view — so a goal outside that
    window answered "no goal … for this tenant". ``board.get`` is the id lookup.
    """
    board = _board(tmp_path)
    goal = board.create(user_id="alice", title="A real goal", status="ready")
    client, pages = _pages_client(monkeypatch, tmp_path)

    calls = []
    real_list = board.list
    monkeypatch.setattr(pages, "_webgate_goal_board", lambda: board)
    monkeypatch.setattr(type(board), "list",
                        lambda self, **kw: (calls.append(kw), real_list(**kw))[1])

    resp = client.post(f"/api/webgate/goals/{goal.id}/pause")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls == [], "the goal verb must not use board.list as a view"


def test_another_tenants_goal_id_is_a_miss_not_a_mutation(monkeypatch, tmp_path):
    board = _board(tmp_path)
    theirs = board.create(user_id="bob", title="Not yours", status="ready")
    client, pages = _pages_client(monkeypatch, tmp_path, user_id="alice")
    monkeypatch.setattr(pages, "_webgate_goal_board", lambda: board)

    resp = client.post(f"/api/webgate/goals/{theirs.id}/cancel")
    assert resp.status_code == 404
    assert board.get(theirs.id).status == "ready"


def test_an_ask_id_is_not_a_goal_id(monkeypatch, tmp_path):
    """An ask and a goal share one table; a goal VERB may only reach a goal."""
    board = _board(tmp_path)
    ask = board.create_ask(user_id="alice", what="Approve it?", force=True)
    client, pages = _pages_client(monkeypatch, tmp_path)
    monkeypatch.setattr(pages, "_webgate_goal_board", lambda: board)

    assert client.post(f"/api/webgate/goals/{ask.id}/cancel").status_code == 404


# --- A7: the goal says which session it runs in ------------------------------ #

def test_a_goal_row_carries_its_session_id(tmp_path):
    """Work › Now draws running goals from the board and live actors from the
    live reader; without this key it cannot tell that the two are one run."""
    import webview.pages as pages
    board = _board(tmp_path)
    goal = board.create(user_id="alice", title="Runs somewhere", status="ready")
    board.stamp_session(goal.id, "sess-77")

    row = pages._goal_dict(board.get(goal.id))
    assert row["session_id"] == "sess-77"


def test_a_goal_with_no_session_says_none_not_empty_string(tmp_path):
    import webview.pages as pages
    board = _board(tmp_path)
    goal = board.create(user_id="alice", title="Never ran", status="ready")
    assert pages._goal_dict(board.get(goal.id))["session_id"] is None


def test_a_legacy_payload_session_id_is_still_found(tmp_path):
    import webview.pages as pages
    board = _board(tmp_path)
    goal = board.create(user_id="alice", title="Older row", status="ready",
                        payload={"run_session_id": "sess-legacy"})
    assert pages._goal_dict(board.get(goal.id))["session_id"] == "sess-legacy"


# --- A8 / E2: pending is the Inbox ------------------------------------------- #

def test_pending_names_a_store_that_refused(monkeypatch, tmp_path):
    """⚠️ Each collector's failure used to be swallowed into an empty
    contribution, so a locked approval store read as "nothing is waiting" on
    the one screen whose whole job is to say what IS."""
    import webview.inbox as inbox
    client, _pages = _pages_client(monkeypatch, tmp_path)

    def _boom(user_id):
        raise OSError("goals.db is locked")

    monkeypatch.setattr(inbox, "_collect_tool_approvals", _boom)
    body = client.get("/api/webgate/pending").json()
    assert body["unreadable_sources"] == ["tool_approvals"]
    assert body["uncertain"] is True
    assert "goals.db is locked" in body["sources"]["tool_approvals"]
    assert body["source_reasons"]["tool_approvals"] == "OSError: goals.db is locked"


def test_pending_and_the_inbox_compose_the_same_body(monkeypatch, tmp_path):
    client, _pages = _pages_client(monkeypatch, tmp_path)
    import webview.inbox as inbox
    monkeypatch.setattr(inbox, "_tenant", lambda request: "alice")
    app = FastAPI()
    app.include_router(inbox.router)
    inbox_body = TestClient(app).get("/api/webgate/inbox").json()
    pending_body = client.get("/api/webgate/pending").json()
    assert pending_body["items"] == inbox_body["items"]
    assert pending_body["count"] == inbox_body["count"]
    assert pending_body["user_id"] == "alice"


# --- A27: an ask can be answered --------------------------------------------- #

def _inbox_client(monkeypatch, tmp_path, user_id="alice"):
    import webview.inbox as inbox
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(inbox, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(inbox, "_tenant", lambda request: user_id)
    app = FastAPI()
    app.include_router(inbox.router)
    return TestClient(app), inbox


def test_an_ask_records_the_owners_answer(monkeypatch, tmp_path):
    """⚠️ An ask is a QUESTION. Until A27 the console could only say yes or no,
    so "which API key should I use?" was answerable only with "approved"."""
    board = _board(tmp_path)
    ask = board.create_ask(user_id="alice", what="Which key?", force=True)
    client, inbox = _inbox_client(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "_goal_board", lambda: board)

    resp = client.post(f"/api/webgate/inbox/ask/{ask.id}/fulfill",
                       json={"answer": "  use the staging key  "})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert board.get(ask.id).payload["answer"] == "use the staging key"


def test_a_refused_decision_records_no_answer(monkeypatch, tmp_path):
    """A decision that did not land must leave no answer on the row."""
    board = _board(tmp_path)
    ask = board.create_ask(user_id="alice", what="Which key?", force=True)
    board.decide_ask(ask.id, user_id="alice", approved=True)  # already closed
    client, inbox = _inbox_client(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "_goal_board", lambda: board)

    resp = client.post(f"/api/webgate/inbox/ask/{ask.id}/decide",
                       json={"answer": "too late"})
    assert resp.json()["ok"] is False
    assert "answer" not in (board.get(ask.id).payload or {})


def test_a_decision_with_no_body_is_still_a_decision(monkeypatch, tmp_path):
    """The answer is optional; a malformed one must not lose the decision."""
    board = _board(tmp_path)
    ask = board.create_ask(user_id="alice", what="Which key?", force=True)
    client, inbox = _inbox_client(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "_goal_board", lambda: board)

    resp = client.post(f"/api/webgate/inbox/ask/{ask.id}/decide")
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_an_answer_is_bounded(monkeypatch, tmp_path):
    from webview.inbox import ANSWER_MAX_CHARS
    board = _board(tmp_path)
    ask = board.create_ask(user_id="alice", what="Which key?", force=True)
    client, inbox = _inbox_client(monkeypatch, tmp_path)
    monkeypatch.setattr(inbox, "_goal_board", lambda: board)

    client.post(f"/api/webgate/inbox/ask/{ask.id}/fulfill",
                json={"answer": "x" * (ANSWER_MAX_CHARS + 500)})
    assert len(board.get(ask.id).payload["answer"]) == ANSWER_MAX_CHARS


# --- A17: a slash verb on cold open ------------------------------------------ #

def test_a_known_verb_is_recognised_before_a_session_is_made():
    from webview.console_commands import looks_like_console_verb
    assert looks_like_console_verb("/halt") is True
    assert looks_like_console_verb("/status now") is True
    assert looks_like_console_verb("/status@robot") is True
    # Prose, an unknown slash, and the two session-CREATING verbs the console
    # has its own controls for, all reach the agent unchanged.
    assert looks_like_console_verb("halt the trading") is False
    assert looks_like_console_verb("/notaverb") is False
    assert looks_like_console_verb("/task do a thing") is False
    assert looks_like_console_verb("/new") is False
    assert looks_like_console_verb("") is False
    assert looks_like_console_verb(None) is False


def test_the_console_owns_the_create_path_before_the_api_tier():
    """Route ORDER is the mechanism: FastAPI takes the first path match."""
    import webview.server as server

    def walk(router):
        for route in getattr(router, "routes", []) or []:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from walk(inner)
            elif getattr(route, "routes", None) and not hasattr(route, "endpoint"):
                yield from walk(route)
            else:
                yield route

    seen = [getattr(r.endpoint, "__name__", "")
            for r in walk(server._fastapi.router)
            if getattr(r, "path", "").endswith("/task/sessions")
            and "POST" in (getattr(r, "methods", None) or set())]
    assert seen[:1] == ["console_create_session"], seen


# --- A33: the money reader uses shared seams --------------------------------- #

def test_the_moves_reader_renders_the_shared_section(monkeypatch, tmp_path):
    """⚠️ The console carried its OWN SQL over ``telemetry_events``, so Money ›
    Moves was a second answer to a question the status snapshot already
    answers — free to drift from the terminal's. It now renders
    ``core.status_snapshot.moves_section`` and only reshapes it for the pane,
    and holds no query of its own at all."""
    import inspect

    import webview.pages_new as mod
    assert not hasattr(mod, "_telemetry_rows"), \
        "the hand-rolled telemetry query must be gone, not kept beside the seam"
    code = "\n".join(line for line in inspect.getsource(mod._moves_body).splitlines()
                     if not line.lstrip().startswith(("#", '"""', "''", "⚠", "*")))
    assert "moves_section" in code
    assert "SELECT" not in code, "the console must not query telemetry itself"
    monkeypatch.setattr(mod.webgate, "data_dir", lambda: str(tmp_path))
    # No store on disk -> the reader RAISES (a read never creates one), and the
    # body renders `unavailable`, never an empty list of moves.
    body = mod._moves_body("alice")
    assert body["moves"] is None and body["unavailable"]


def test_the_moves_reader_reads_the_real_event_log(monkeypatch, tmp_path):
    import sqlite3

    import webview.pages_new as mod
    from core.event_log import telemetry_db_path
    monkeypatch.setattr(mod.webgate, "data_dir", lambda: str(tmp_path))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    path = telemetry_db_path(str(tmp_path))
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (ts REAL, kind TEXT, "
                "user_id TEXT, attrs TEXT)")
    con.execute("INSERT INTO telemetry_events VALUES (1.0, 'wallet_spend', "
                "'alice', ?)", (json.dumps({"action": "swap", "usd": 1.5}),))
    con.commit()
    con.close()

    body = mod._moves_body("alice")
    assert body["unavailable"] is None
    assert [m["action"] for m in body["moves"]] == ["swap"]


# --- A35: the outstanding total is over every pending row -------------------- #

def test_the_outstanding_total_counts_every_pending_row():
    """⚠️ The console summed the 50-row page it was sent and printed the result
    as "outstanding" — a figure that stops growing at the 51st invoice."""
    from webview.pages import _outstanding_total
    rows = [{"amount_usd": 10.0}, {"amount_usd": 2.5}]
    assert _outstanding_total(rows) == (12.5, 0)


def test_an_unpriced_pending_row_is_counted_never_treated_as_zero():
    from webview.pages import _outstanding_total
    total, unpriced = _outstanding_total([{"amount_usd": 10.0},
                                          {"amount_usd": None}])
    assert (total, unpriced) == (10.0, 1)


# --- A44: the console can size a goal's run ---------------------------------- #

@pytest.mark.parametrize("raw,expected", [(None, None), ("", None),
                                          (6, 6), (60, 60), ("30", 30)])
def test_an_accepted_step_budget(raw, expected):
    from webview.pages_new import _as_max_steps
    value, error = _as_max_steps(raw)
    assert (value, error) == (expected, None)


@pytest.mark.parametrize("raw", [5, 61, 0, -1, "lots"])
def test_a_bad_step_budget_is_refused_never_clamped(raw):
    """⚠️ A priority is a preference, so clamping it loses nothing. A step
    budget is what the owner believes the run will cost — quietly halving it
    produces a run that stops short for a reason nobody was told."""
    from webview.pages_new import _as_max_steps
    value, error = _as_max_steps(raw)
    assert value is None and error


def test_a_goal_created_with_a_budget_carries_it_into_the_payload(monkeypatch,
                                                                  tmp_path):
    import webview.pages as pages
    import webview.pages_new as mod
    board = _board(tmp_path)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    monkeypatch.setattr(pages, "_webgate_goal_board", lambda: board)
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)

    resp = client.post("/api/webgate/goals",
                       json={"title": "Sized work", "max_steps": 45})
    assert resp.status_code == 201, resp.text
    assert board.get(resp.json()["id"]).payload["max_steps"] == 45

    bad = client.post("/api/webgate/goals",
                      json={"title": "Too big", "max_steps": 900})
    assert bad.status_code == 400


# --- 2026-09-21 revalidation: residue found re-reading the audit commit ------ #
#
# Each of these pins a defect the audit's own fix left behind. They are here
# rather than in a new module because they are the same section's findings.


def test_the_cold_open_verb_answers_inline_and_creates_no_session(monkeypatch):
    """A17, end to end. The route ORDER test next door proves the console owns
    the path; this proves what it does with it — a known owner verb comes back
    as ``command_reply`` and the real creator is never called."""
    import webview.console_commands as cc
    import webview.pages as pages

    created = []

    async def never(request_body, req, agent=None):  # pragma: no cover - asserted
        created.append(request_body)
        raise AssertionError("a verb must not create a session")

    async def answered(task_agent, clean_id, user_id, text):
        return f"answered {text} for {user_id}"

    import api.task_http_api as thp
    monkeypatch.setattr(thp, "create_session", never)
    monkeypatch.setattr(cc, "maybe_handle_console_command", answered)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")

    app = FastAPI()
    router = cc.build_console_create_router()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[thp.get_task_agent] = lambda: object()
    client = TestClient(app)

    resp = client.post("/api/task/sessions", json={"task": "/status"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True,
                           "command_reply": "answered /status for alice"}
    assert created == []


def test_a_task_that_is_not_a_verb_still_creates_a_session(monkeypatch):
    """The short-circuit is a HOP, not a filter: ordinary prose must reach the
    api tier's own creator with the body it was sent, untouched."""
    import webview.console_commands as cc
    import webview.pages as pages
    import api.task_http_api as thp
    from fastapi.responses import JSONResponse

    seen = {}

    async def create(request_body, req, agent=None):
        seen["body"] = request_body
        return JSONResponse({"success": True, "session_id": "sess-1"})

    monkeypatch.setattr(thp, "create_session", create)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")

    app = FastAPI()
    app.include_router(cc.build_console_create_router(), prefix="/api")
    app.dependency_overrides[thp.get_task_agent] = lambda: object()
    client = TestClient(app)

    resp = client.post("/api/task/sessions",
                       json={"task": "write me a report", "auto_start": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_id"] == "sess-1"
    assert seen["body"] == {"task": "write me a report", "auto_start": True}


def test_a_verb_the_plane_declines_falls_through_to_creation(monkeypatch):
    """``maybe_handle_console_command`` is fail-open: when it returns ``None``
    (a two-service console with no agent, or a verb it cannot run here) the
    message must still become a session rather than vanishing."""
    import webview.console_commands as cc
    import webview.pages as pages
    import api.task_http_api as thp
    from fastapi.responses import JSONResponse

    async def declines(task_agent, clean_id, user_id, text):
        return None

    async def create(request_body, req, agent=None):
        return JSONResponse({"success": True, "session_id": "sess-2"})

    monkeypatch.setattr(cc, "maybe_handle_console_command", declines)
    monkeypatch.setattr(thp, "create_session", create)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")

    app = FastAPI()
    app.include_router(cc.build_console_create_router(), prefix="/api")
    app.dependency_overrides[thp.get_task_agent] = lambda: object()
    client = TestClient(app)

    resp = client.post("/api/task/sessions", json={"task": "/status"})
    assert resp.json()["session_id"] == "sess-2"


def test_the_task_mount_records_a_failure_that_is_not_an_import_error():
    """A43 residue. The auth mount was widened to ``Exception`` and the task
    mount beside it was not — and that block RUNS
    ``build_console_create_router()``, so a moved symbol or a bad signature
    escaped it entirely instead of landing in ``UNMOUNTED_ROUTERS``."""
    import ast
    import inspect

    import webview.server as server
    source = inspect.getsource(server)
    tree = ast.parse(source)
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "build_console_create_router" not in body:
            continue
        for handler in node.handlers:
            handlers.append(getattr(handler.type, "id", None))
    assert handlers == ["Exception"], handlers


def test_the_head_pause_control_is_drawn_only_where_it_can_act(monkeypatch):
    """A2 residue. ``POST /api/webgate/pause`` refuses every multitenant caller
    (the pause record is instance-wide), and the button was gated on read-only
    ALONE — so an authenticated tenant saw a Pause control on all five
    destinations that answered 403 on every click."""
    import webview.pages_new as pn
    import webview.webgate as wg

    monkeypatch.setattr(wg, "posture", lambda: "multitenant")
    assert pn._owner_console() is False
    monkeypatch.setattr(wg, "posture", lambda: "own_ops")
    assert pn._owner_console() is True
    monkeypatch.setattr(wg, "posture", lambda: "local")
    assert pn._owner_console() is True


def test_the_pause_refusal_and_the_pause_button_read_one_predicate(monkeypatch):
    """Two rules for one question is how a control and its refusal drift."""
    import webview.pages as pages
    import webview.pages_new as pn
    import webview.webgate as wg
    from fastapi import HTTPException

    monkeypatch.setattr(wg, "is_owner_console", lambda: False)
    assert pn._owner_console() is False
    with pytest.raises(HTTPException) as refusal:
        pages._owner_console_required()
    assert refusal.value.status_code == 403

    monkeypatch.setattr(wg, "is_owner_console", lambda: True)
    assert pn._owner_console() is True
    pages._owner_console_required()  # does not raise


def test_the_shell_omits_the_pause_button_without_the_owner_console(monkeypatch):
    """The template half of the same rule, rendered."""
    import webview.pages_new as pn
    template = pn._TEMPLATES.get_template("shell.html")
    base = {"request": None, "nav_items": [], "nav_current": "work",
            "page_title": "t", "wordmark": "Rob", "pause_headline": "",
            "waiting_line": "", "inbox_badge": "", "inbox_aria": "",
            "inbox_partial": False, "show_logout": False}
    allowed = template.render(**base, read_only=False, owner_console=True)
    assert 'id="head-pause"' in allowed
    for ctx in ({"read_only": True, "owner_console": True},
                {"read_only": False, "owner_console": False},
                {"read_only": True, "owner_console": False}):
        assert 'id="head-pause"' not in template.render(**base, **ctx), ctx


def test_moves_drops_creations_and_keeps_the_explorer_link(monkeypatch, tmp_path):
    """The console renders the SHARED section and reshapes it. Two facts the
    reshape owns: a creation belongs to its own tier (drawing it here too shows
    one act twice), and the row's explorer link survives the rename."""
    import sqlite3

    import webview.pages_new as mod
    from core.event_log import telemetry_db_path
    from core.status_snapshot import CREATION_VERBS

    monkeypatch.setattr(mod.webgate, "data_dir", lambda: str(tmp_path))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(str(tmp_path), "telemetry_events.db"))
    path = telemetry_db_path(str(tmp_path))
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (ts REAL, kind TEXT, "
                "user_id TEXT, attrs TEXT)")
    rows = [
        (3.0, json.dumps({"action": "swap", "amount_usd": 2.5, "chain": "base",
                          "result_ref": "0xabc", "counterparty": "0xdef",
                          "asset": "USDC"})),
        (2.0, json.dumps({"action": sorted(CREATION_VERBS)[0], "chain": "base",
                          "result_ref": "0x999"})),
        (1.0, "{not json"),
    ]
    for ts, attrs in rows:
        con.execute("INSERT INTO telemetry_events VALUES (?, 'wallet_spend', "
                    "'alice', ?)", (ts, attrs))
    con.commit()
    con.close()

    body = mod._moves_body("alice")
    assert body["unavailable"] is None
    assert [m["action"] for m in body["moves"]] == ["swap"]
    move = body["moves"][0]
    assert move["amount_usd"] == 2.5 and move["counterparty"] == "0xdef"
    assert move["url"] and "0xabc" in move["url"]
    # ⚠️ An unparseable row is COUNTED, never dropped: "one move" must not come
    # to mean "one move I could read".
    assert body["unreadable_rows"] == 1


# --- cross-tier: the credentials and the roles the API tier renamed ---------- #

def test_the_proxy_forwards_the_operator_token_under_its_own_header(monkeypatch):
    """⚠️ It sent ``API_AUTH_TOKEN`` as ``X-API-KEY``. The 2026-09-21 API work
    made that header the per-user ``rob_xxx`` validator's and moved the operator
    credential to ``X-Service-Token`` (role ``service``); the old spelling is
    accepted for ONE release with a deprecation WARN. The console was therefore
    one release from every proxied chat message 401-ing, with nothing in its own
    logs to explain it."""
    from api.auth_constants import SERVICE_TOKEN_HEADER
    import webview.server as server

    class _Req:
        headers = {}
        cookies = {}

    monkeypatch.setenv("API_AUTH_TOKEN", "operator-secret")
    headers = server._api_proxy_auth_headers(_Req())
    assert headers == {SERVICE_TOKEN_HEADER: "operator-secret"}
    assert "X-API-KEY" not in headers


def test_a_browser_credential_still_wins_over_the_operator_token(monkeypatch):
    """The operator token is the FALLBACK; a real caller identity comes first."""
    import webview.server as server

    monkeypatch.setenv("API_AUTH_TOKEN", "operator-secret")

    class _Bearer:
        headers = {"Authorization": "Bearer abc"}
        cookies = {}

    class _Cookie:
        headers = {}
        cookies = {"auth_token": "jwt-xyz"}

    assert server._api_proxy_auth_headers(_Bearer()) == {"Authorization": "Bearer abc"}
    assert server._api_proxy_auth_headers(_Cookie()) == {"Authorization": "Bearer jwt-xyz"}


class _State:
    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


class _Request:
    def __init__(self, **kw):
        self.state = _State(**kw)


def test_admin_is_the_role_predicate_not_a_cached_boolean():
    """⚠️ `activity.py` and `posture_routes.py` read ``request.state.is_admin``
    — the second admin truth the API tier removed. That flag is written by THIS
    process's auth middleware and nothing else, so a request identified on any
    other path carries a ROLE and no flag, and both readers answered "not an
    admin" for a real one (a 404 and a 303, neither of them explained)."""
    from webview import webgate

    # A role, no flag: the case that used to fail.
    assert webgate.request_is_admin(_Request(role="admin")) is True
    assert webgate.request_is_admin(_Request(role="owner")) is True
    assert webgate.request_is_admin(_Request(role="user")) is False
    # The flag alone still counts — it also carries admin-BY-WALLET, which no
    # role string can express.
    assert webgate.request_is_admin(_Request(role="user", is_admin=True)) is True
    # Fail-CLOSED.
    assert webgate.request_is_admin(_Request()) is False
    assert webgate.request_is_admin(object()) is False


def test_both_admin_readers_go_through_the_one_predicate():
    """A grep-level pin: neither reader may reach for the raw flag again."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    for name in ("webview/activity.py", "webview/posture_routes.py"):
        source = (repo / name).read_text(encoding="utf-8")
        assert "request_is_admin(" in source, name
        for line in source.splitlines():
            code = line.split("#", 1)[0]
            assert 'getattr(state, "is_admin"' not in code, f"{name}: {line}"
            assert 'getattr(request.state, "is_admin"' not in code, f"{name}: {line}"


# --- the instance-wide controls are drawn where they can act ----------------- #

def test_an_app_card_on_a_tenant_seat_carries_no_verbs_but_still_says_so():
    """``inbox._decide`` refuses an app decision for every multitenant caller.
    The card must still be SHOWN — what is waiting is a fact for any seat — with
    the verbs replaced by one sentence naming where the decision is taken."""
    from webview.pages_new import _card
    item = {"kind": "app", "id": "slug1", "title": "deploy site",
            "actions": ["approve", "reject"]}

    owner = _card(item, owner_console=True)
    assert [a["verb"] for a in owner["rendered_actions"]] == ["decide", "reject"]
    assert "decide_elsewhere" not in owner

    tenant = _card(item, owner_console=False)
    assert tenant["rendered_actions"] == []
    assert tenant["decide_elsewhere"], "a card with no verbs must say why"
    assert tenant["title"] == "deploy site", "the item is still shown"


def test_a_non_app_card_keeps_its_verbs_on_every_seat():
    """Only the INSTANCE-wide kind is gated — an ask is the tenant's own."""
    from webview.pages_new import _card
    ask = _card({"kind": "ask", "id": "a1", "title": "Which key?",
                 "actions": ["fulfill", "reject"]}, owner_console=False)
    assert [a["verb"] for a in ask["rendered_actions"]] == ["fulfill", "reject"]
    assert "decide_elsewhere" not in ask


def test_the_apps_pane_is_told_the_seats_posture():
    """The template hands `webgate.is_owner_console()` to work-apps.js the way
    it hands read-only, so Stop is drawn only where `apps_routes._decide` will
    take it."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    work = (repo / "webview/templates/work.html").read_text(encoding="utf-8")
    assert 'data-owner_console="{{ 1 if owner_console else 0 }}"' in work
    js = (repo / "webview/static/app/work-apps.js").read_text(encoding="utf-8")
    assert 'copy.owner_console === "1"' in js
    assert "if (!readOnly && canDecide) {" in js


# --- the invoice vocabulary is the store's ----------------------------------- #

def test_the_console_filters_on_every_status_the_store_can_hold():
    """⚠️ A hand-written three-word tuple 400'd a request for ``refund_due``
    (money taken, nothing delivered) or ``settling`` — the two rows an owner
    most needs to list — and named three statuses as if they were all of them."""
    from modules.x402.invoicing import INVOICE_STATUSES
    from webview.pages import _invoice_statuses
    assert _invoice_statuses() == tuple(INVOICE_STATUSES)
    for status in ("refund_due", "settling", "settled_no_tx"):
        assert status in _invoice_statuses()


def test_an_unknown_invoice_status_is_still_refused(monkeypatch, tmp_path):
    client, _pages = _pages_client(monkeypatch, tmp_path)
    resp = client.get("/api/webgate/invoices?status=nonsense")
    assert resp.status_code == 400
    assert "refund_due" in resp.json()["error"]
