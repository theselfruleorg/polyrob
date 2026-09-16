"""043 W1 — same-origin CSRF check on every console mutation.

The console authenticates with an ambient cookie (`auth_token`, 7 days on
own_ops). Every `POST /api/webgate/*` was therefore reachable from any page the
owner happened to open: pause the agent, settle an invoice, approve an app,
promote a pending proposal, write a config flag. The only CSRF defence in the
tree was the owner-LOGIN form's double-submit token, which protects the one POST
that has no session yet.

`webgate.csrf_guard` rides the same `MUTATION_DEPS` list as the read-only guard,
so the two live in one place and every mutating route gets both.

Threat model, stated so the "no header" branch is not mistaken for a hole: a
browser attaches `Origin` to EVERY cross-origin mutation (fetch, XHR and form
POST alike) and the page cannot suppress it — that is the whole basis of this
check. A request with NEITHER `Origin` NOR `Referer` is therefore not a
browser-driven cross-site request: it is a machine client (the telemetry push to
`/api/internal/emit`, the agent's stream POST, a bearer-token API caller,
curl/CI), and those carry no ambient cookie to abuse.
"""
import types

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, URL


def _req(method="POST", url="https://console.example.com/api/webgate/pause", **headers):
    return types.SimpleNamespace(method=method, url=URL(url),
                                 headers=Headers(headers))


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _guard(request):
    from webview import webgate
    return _run(webgate.csrf_guard(request))


def test_a_foreign_origin_is_refused():
    with pytest.raises(HTTPException) as exc:
        _guard(_req(origin="https://evil.example"))
    assert exc.value.status_code == 403
    assert "origin" in exc.value.detail.lower()


def test_the_same_origin_passes():
    _guard(_req(origin="https://console.example.com"))


def test_a_foreign_referer_is_refused_when_origin_is_absent():
    with pytest.raises(HTTPException) as exc:
        _guard(_req(referer="https://evil.example/some/page"))
    assert exc.value.status_code == 403


def test_the_same_origin_referer_passes():
    _guard(_req(referer="https://console.example.com/autonomy"))


def test_origin_wins_over_referer():
    """A page can relax its own Referer-Policy; it cannot forge Origin."""
    with pytest.raises(HTTPException):
        _guard(_req(origin="https://evil.example",
                    referer="https://console.example.com/autonomy"))


def test_a_get_is_never_checked():
    _guard(_req(method="GET", origin="https://evil.example"))
    _guard(_req(method="HEAD", origin="https://evil.example"))


def test_a_machine_client_with_no_origin_and_no_referer_passes():
    """The telemetry push, the agent's stream POST, curl/CI. No browser can
    reach here: `Origin` is attached by the browser, not the page."""
    _guard(_req())
    _guard(_req(authorization="Bearer rob_abc"))


def test_a_port_mismatch_on_the_same_host_is_refused():
    with pytest.raises(HTTPException):
        _guard(_req(url="http://127.0.0.1:5050/api/webgate/pause",
                    origin="http://127.0.0.1:8080"))


def test_the_default_port_is_not_a_mismatch():
    """`https://host` and a request whose URL carries no explicit port are the
    same origin — a strict string compare would 403 every proxied console."""
    _guard(_req(url="https://console.example.com/api/webgate/pause",
                origin="https://console.example.com"))


def test_both_guards_ride_the_one_dependency_list():
    from webview import webgate
    names = [d.dependency.__name__ for d in webgate.MUTATION_DEPS]
    assert names == ["read_only_guard", "csrf_guard"]


def test_a_cross_site_post_to_a_real_route_is_refused(monkeypatch, tmp_path):
    """End to end through the router, not just the guard function."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    client = TestClient(app, base_url="http://console.test")
    hostile = client.post("/api/webgate/pause", json={},
                          headers={"Origin": "https://evil.example"})
    assert hostile.status_code == 403
    friendly = client.post("/api/webgate/pause", json={},
                           headers={"Origin": "http://console.test"})
    assert friendly.status_code != 403


def test_a_cookie_bearing_request_must_state_its_origin():
    """The header-less pass exists for MACHINE clients, and its justification is
    that they carry no ambient credential — so the pass requires that to be
    true. The telemetry rail sends no cookie; a browser that somehow reaches
    here with one is held to the origin check."""
    with pytest.raises(HTTPException) as exc:
        _guard(_req(cookie="auth_token=abc"))
    assert exc.value.status_code == 403
    assert "Origin" in exc.value.detail


def test_a_cookie_bearing_request_with_a_good_origin_passes():
    _guard(_req(origin="https://console.example.com", cookie="auth_token=abc"))
