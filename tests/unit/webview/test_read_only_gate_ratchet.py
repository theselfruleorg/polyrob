"""043 W3 — ONE read-only check in ``webview/``, ratcheted.

Before this ratchet the console carried FIVE hand-rolled read-only refusals
(``pages._mutation_refused``, ``pages._pfp_setup_refused``, four inline
``webgate.read_only()`` tests in ``pages.py``, two in ``server.py``, plus the
task-router-only ``_task_router_read_only_guard``) and TWENTY mutating routes,
so "is this route refused on a read-only console?" had no single answer — three
of the twenty carried no check at all.

The one answer is ``webview.webgate.read_only_guard``, mounted as a route
``dependencies=`` (via ``webgate.MUTATION_DEPS``). This module is the structural
proof:

- every ``post``/``patch``/``put``/``delete`` route DEFINED in ``webview/``
  carries it (including the posture-gated ``_posture_post`` registrations);
- every router mounted into the console from OUTSIDE ``webview/`` (the api-tier
  auth + task routers, whose routes this AST scan cannot see) carries it at the
  ``include_router`` seam;
- no hand-rolled check survives.
"""
import ast
import pathlib

import pytest

_WEBVIEW = pathlib.Path(__file__).resolve().parents[3] / "webview"

_MUTATING = ("post", "patch", "put", "delete")
#: The guard is referenced either directly or through the shared
#: ``webgate.MUTATION_DEPS`` list (read-only guard + CSRF guard, B3).
_GUARD_TOKENS = ("read_only_guard", "MUTATION_DEPS")


def _py_files():
    return sorted(_WEBVIEW.glob("*.py"))


def _decorator_is_mutating_route(d: ast.AST) -> bool:
    """``@router.post(...)`` / ``@_fastapi.patch(...)`` / ``@_posture_post(...)``."""
    if not isinstance(d, ast.Call):
        return False
    if isinstance(d.func, ast.Attribute) and d.func.attr in _MUTATING:
        return True
    if isinstance(d.func, ast.Name):
        if d.func.id == "_posture_post":
            return True
        # _posture_route("post", …) — the generic posture-gated registrar.
        if d.func.id == "_posture_route" and d.args:
            first = d.args[0]
            return isinstance(first, ast.Constant) and first.value in _MUTATING
    return False


def _guarded(call: ast.Call) -> bool:
    kws = {k.arg: k for k in call.keywords if k.arg}
    dep = kws.get("dependencies")
    if dep is None:
        return False
    rendered = ast.unparse(dep.value)
    return any(token in rendered for token in _GUARD_TOKENS)


def _mutating_routes_without_guard():
    out = []
    for f in _py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for d in node.decorator_list:
                if not _decorator_is_mutating_route(d):
                    continue
                if not _guarded(d):
                    out.append(f"{f.name}:{node.lineno} {node.name}")
    return out


def _foreign_router_mounts_without_guard():
    """``include_router(x)`` where ``x`` was imported from OUTSIDE ``webview``.

    Those routes are defined in another package (``api/…``), so the decorator
    scan above cannot see them — the mount seam is where the console's own
    read-only posture has to be applied.
    """
    out = []
    for f in _py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        foreign = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("webview"):
                    continue
                for alias in node.names:
                    foreign.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "include_router"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Name) and first.id in foreign):
                continue
            if not _guarded(node):
                out.append(f"{f.name}:{node.lineno} include_router({first.id})")
    return out


def test_every_mutating_route_carries_the_one_guard():
    assert _mutating_routes_without_guard() == [], (
        "every mutating console route must carry "
        "`dependencies=webgate.MUTATION_DEPS` (webgate.read_only_guard) — "
        "a hand-rolled read_only() check inside the handler is not the one gate"
    )


def test_foreign_router_mounts_carry_the_one_guard():
    assert _foreign_router_mounts_without_guard() == [], (
        "a router mounted from outside webview/ (api.auth_endpoints, "
        "api.task_http_api) must carry the guard at the include_router seam — "
        "its own routes know nothing about the console's read-only posture"
    )


#: The hand-rolled refusals W3 replaced. Matched against real CODE (defs,
#: calls, attribute access) — never against prose, so the history above can
#: keep naming them.
_DEAD_GUARDS = ("_mutation_refused", "_pfp_setup_refused",
                "_task_router_read_only_guard")


def _dead_guard_references():
    out = []
    for f in _py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name in _DEAD_GUARDS:
                out.append(f"{f.name}:{getattr(node, 'lineno', '?')} {name}")
    return sorted(set(out))


def test_no_hand_rolled_read_only_checks_remain():
    assert _dead_guard_references() == [], (
        "the hand-rolled read-only refusals are replaced by webgate.read_only_guard")


def _mutating_handlers_reading_read_only():
    """Mutating route handlers that still branch on ``read_only()`` themselves.

    A GET may legitimately read the flag — to hide a button in a page context,
    or (``api_screenshot``) to skip a write SIDE EFFECT. A mutating handler may
    not: its refusal is ``read_only_guard``'s decision, taken before the body
    runs, so a second in-body check is either dead code or a divergent rule.
    """
    found = []
    for f in _py_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not any(_decorator_is_mutating_route(d) for d in node.decorator_list):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub) in (
                        "webgate.read_only()", "read_only()"):
                    found.append(f"{f.name}:{node.lineno} {node.name}")
    return sorted(set(found))


def test_no_mutating_handler_re_checks_read_only():
    assert _mutating_handlers_reading_read_only() == [], (
        "a mutating handler must not re-check read_only() — read_only_guard "
        "already refused the request before the body ran")


# --- behaviour: the guard refuses, and stays loggable-in -------------------- #

def _guard_request(method: str, path: str = "/api/webgate/pause"):
    import types
    from starlette.datastructures import URL
    req = types.SimpleNamespace(method=method, url=URL(f"http://console.test{path}"))
    return req


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_guard_refuses_a_mutation_on_a_read_only_console(monkeypatch):
    from fastapi import HTTPException
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    with pytest.raises(HTTPException) as exc:
        _run(webgate.read_only_guard(_guard_request("POST")))
    assert exc.value.status_code == 403
    assert "WEBVIEW_READ_ONLY" in exc.value.detail


def test_guard_allows_reads_and_a_writable_console(monkeypatch):
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    _run(webgate.read_only_guard(_guard_request("GET")))        # a read is a read
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "false")
    _run(webgate.read_only_guard(_guard_request("POST")))       # writable console


def test_a_read_only_console_stays_loggable_in(monkeypatch):
    """Prod runs own_ops + WEBVIEW_READ_ONLY: refusing the login POST would lock
    the owner out of the monitoring console. Signing in mutates the CALLER's
    cookie, never console or agent state."""
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    _run(webgate.read_only_guard(_guard_request("POST", "/owner-login")))
    _run(webgate.read_only_guard(_guard_request("POST", "/api/auth/verify")))


# --- the exemption set is EXACT, and it is the right set -------------------- #

def test_a_read_only_console_cannot_mint_an_api_key(monkeypatch):
    """The exemption used to be the PREFIX ``/api/auth/``, which also covered
    ``POST /api/auth/api-keys`` and ``DELETE /api/auth/api-keys/{prefix}`` — a
    read-only console could still mint and revoke durable A2A credentials."""
    from fastapi import HTTPException
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    for path in ("/api/auth/api-keys", "/api/auth/api-keys/rob_abc"):
        with pytest.raises(HTTPException) as exc:
            _run(webgate.read_only_guard(_guard_request("POST", path)))
        assert exc.value.status_code == 403, path


def test_the_wallet_login_pair_still_answers(monkeypatch):
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    _run(webgate.read_only_guard(_guard_request("POST", "/api/auth/nonce")))
    _run(webgate.read_only_guard(_guard_request("POST", "/api/auth/verify")))


def test_the_loopback_rails_still_answer(monkeypatch):
    """``/api/internal/emit`` (the agent's telemetry fast-push) and the
    per-session stream POST carry the LIVE feed a monitoring console exists to
    display. Refusing them under WEBVIEW_READ_ONLY — prod's posture — would kill
    the telemetry fast path and token streaming in the deployment that only
    watches. Each keeps its own 127.0.0.1 check (see test_loopback_only below)."""
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    _run(webgate.read_only_guard(_guard_request("POST", "/api/internal/emit")))
    _run(webgate.read_only_guard(
        _guard_request("POST", "/api/webview/sessions/sess-1/stream")))


def test_the_exemptions_do_not_match_a_deeper_path(monkeypatch):
    """Boundary: the one parameterised exemption is anchored and single-segment,
    and the exact set is equality — a prefix is how the api-key hole got in."""
    from fastapi import HTTPException
    from webview import webgate
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    for path in ("/api/webview/sessions/a/b/stream",
                 "/api/webview/sessions/s-1/stream/extra",
                 "/api/internal/emit/other",
                 "/owner-login/steal"):
        with pytest.raises(HTTPException) as exc:
            _run(webgate.read_only_guard(_guard_request("POST", path)))
        assert exc.value.status_code == 403, path


def test_the_loopback_rails_are_still_loopback_only(monkeypatch):
    """Exempting them from the read-only refusal must not exempt them from the
    check that makes them safe to expose at all."""
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    client = TestClient(srv._fastapi, client=("203.0.113.7", 1234))
    assert client.post("/api/internal/emit", json={"session_id": "s", "event": {}}
                       ).status_code == 403
    assert client.post("/api/webview/sessions/s-1/stream", json={"chunk": "x"}
                       ).status_code == 403
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    importlib.reload(wg)
    importlib.reload(srv)
