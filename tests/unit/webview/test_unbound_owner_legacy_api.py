"""043 residue W1 — the LEGACY console readers refuse an unbound console too.

⚠️ The defect. ``webview/inbox.py::unbound_console()`` is the ONE predicate for
"this own_ops console was never told whose data it shows", and the final fix
round of phase 1 wired it into the Inbox endpoint and the new shell. It was
never wired into ``webview/pages.py::_effective_user_id`` — the tenant seam the
24 LEGACY readers use (``/api/webgate/goals|cron|apps|memory|identity|ledger|
knowledge/*`` …). Those kept answering for whatever tenant the unbound fallback
names (``webgate.local_owner_id()`` warns once and answers it — the instance id
``polyrob`` then, the local tenant since 2026-09-15), so
the Inbox said "I cannot read this" one click away from an Autonomy page
rendering an empty goal board as a FACT.

Same rule as the Inbox, for the same reason: a status surface may be
incomplete; it may never be confident and wrong.

Postures other than ``own_ops`` are untouched — ``unbound_console()`` already
returns ``None`` for ``local`` (exactly one owner by construction) and for
``multitenant`` (the tenant comes from the AUTHENTICATED caller, and refusing
there would lock every tenant out over a binding their console does not use).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _own_ops_console(monkeypatch, tmp_path):
    """A read-only own_ops console with NO owner in the environment.

    This is prod's own shape: a monitoring console beside a headless agent.
    The dev box may carry a real owner id, so every source of one is removed.
    """
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    # Never read or CREATE a store in the developer's real data home.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))


@pytest.fixture
def client(monkeypatch):
    import webview.apps_routes as apps_routes
    import webview.pages as pages
    # Both readers gate on their subsystem flag BEFORE the tenant, and "cron is
    # off" is true of every tenant. Turn them on so the tenant seam is what the
    # request reaches.
    monkeypatch.setattr("webview.pages.AutonomyConfig.goals_enabled", lambda: True)
    monkeypatch.setattr(pages, "_cron_enabled", lambda: True)
    app = FastAPI()
    app.include_router(pages.router)
    app.include_router(apps_routes.router)
    return TestClient(app)


LEGACY_READS = ("/api/webgate/goals", "/api/webgate/cron", "/api/webgate/apps")


# --- the refusal ------------------------------------------------------------- #

@pytest.mark.parametrize("path", LEGACY_READS)
def test_an_unbound_console_refuses_the_legacy_read(client, path):
    resp = client.get(path)
    assert resp.status_code == 403, (path, resp.status_code, resp.text)


@pytest.mark.parametrize("path", LEGACY_READS)
def test_the_refusal_names_the_remedy(client, path):
    """An operator reads this one in a terminal, where the variable name is the
    useful half — and it is the SAME SENTENCE the Inbox endpoint raises.

    Pinned by equality, not by ``in``: a substring check leaves the REASON half
    free to drift, and "one sentence, one remedy, wherever the console refuses"
    is the whole point of having a single predicate."""
    import webview.inbox as inbox
    detail = client.get(path).json()["detail"]
    assert detail == f"{inbox.unbound_console()} {inbox.UNBOUND_REMEDY}"
    assert "POLYROB_OWNER_USER_ID" in detail


@pytest.mark.parametrize("path", LEGACY_READS)
def test_the_refusal_is_never_an_empty_list(client, path):
    """The whole defect: a 200 carrying ``[]`` reads as "you have none"."""
    body = client.get(path).json()
    assert "goals" not in body and "jobs" not in body and "apps" not in body


# --- the bound console is unchanged ------------------------------------------ #

@pytest.mark.parametrize("path", LEGACY_READS)
def test_a_bound_console_reads_its_stores_normally(client, monkeypatch, path):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.text)


def test_a_bound_console_answers_for_the_bound_owner(client, monkeypatch):
    """Not just "a 200" — the 200 is scoped to the owner that was bound."""
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")

    class Req:
        state = type("S", (), {})()

    assert pages._effective_user_id(Req()) == "rob"


# --- the other two postures -------------------------------------------------- #

def test_local_is_exempt_because_it_has_exactly_one_owner(monkeypatch):
    """It keeps ANSWERING, and answers the one thing it answered before: whatever
    ``local_owner_id()`` resolves to. The expectation is the FUNCTION, not a
    literal — the unbound fallback tenant is that function's business, and a
    literal here would pin this test to a value it does not own."""
    import webview.pages as pages
    import webview.webgate as webgate
    monkeypatch.setenv("POLYROB_POSTURE", "local")

    class Req:
        state = type("S", (), {})()

    assert pages._effective_user_id(Req()) == webgate.local_owner_id()


def test_multitenant_still_refuses_for_ITS_own_reason(monkeypatch):
    """⚠️ The unbound-owner refusal must not displace the multitenant one: a
    multitenant console never consults the owner binding, so refusing there
    would lock every AUTHENTICATED tenant out of their own data."""
    import webview.pages as pages
    from fastapi import HTTPException
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")

    class Req:
        state = type("S", (), {})()

    with pytest.raises(HTTPException) as exc:
        pages._effective_user_id(Req())
    assert "tenant identity" in str(exc.value.detail).lower()
    assert "POLYROB_OWNER_USER_ID" not in str(exc.value.detail)


def test_an_authenticated_multitenant_caller_is_unaffected(monkeypatch):
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")

    class Req:
        state = type("S", (), {"user_id": "tenant-7"})()

    assert pages._effective_user_id(Req()) == "tenant-7"


# --- one predicate, not two -------------------------------------------------- #

def test_the_seam_asks_inbox_not_a_second_copy_of_the_rule(client, monkeypatch):
    """There is ONE unbound-console predicate. Silence it and the legacy reads
    answer again — which is how a future posture change reaches both seats at
    once instead of one of them."""
    import webview.inbox as inbox
    monkeypatch.setattr(inbox, "unbound_console", lambda: None)
    assert client.get("/api/webgate/goals").status_code == 200


# --- the ONE exception: /doctor must REPORT the binding, not refuse over it --- #

def test_doctor_answers_200_and_says_the_owner_is_unbound(client):
    """⚠️ Every other legacy read 403s here; `/doctor` must not.

    It is the ONE HTTP report of the binding state, and it refuses BECAUSE of
    that state — a diagnostic that conceals the very fact it exists to report.
    It is also the endpoint the console write-flip is verified with, so a 403
    makes the verification unusable in exactly the failure it would diagnose.
    """
    resp = client.get("/api/webgate/doctor")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner_bound"] is False
    assert body["owner_unbound_reason"], "false without a reason is half an answer"
    assert "POLYROB_OWNER_USER_ID" in body["owner_unbound_reason"]


def test_doctor_reports_health_unavailable_rather_than_healthy(client):
    """No tenant means no health snapshot. It must say so — never render an
    empty-but-green report for a console that could not name whose health it is."""
    body = client.get("/api/webgate/doctor").json()
    assert body["health"]["overall"] == "unavailable"
    assert body["health"]["items"] == []
    assert any("unavailable" in ln for ln in body["health"]["lines"])
    assert body["status_lines"] == []


def test_doctor_still_carries_the_fields_that_do_not_need_a_tenant(client):
    """version/provider/model/instance/unmounted routers are console facts, not
    tenant facts: refusing them over a missing owner loses real diagnostics."""
    body = client.get("/api/webgate/doctor").json()
    for key in ("checks", "instance_id", "version", "provider", "model",
                "memory_backend", "unmounted_routers"):
        assert key in body, key


def test_a_bound_doctor_is_unchanged(client, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    resp = client.get("/api/webgate/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_bound"] is True
    assert body["owner_unbound_reason"] == ""
    assert body["health"]["overall"] != "unavailable"


def test_doctor_still_refuses_an_unauthenticated_multitenant_caller(monkeypatch):
    """The multitenant 403 is a DIFFERENT refusal — no authenticated identity,
    not an unbound owner — and must survive this exemption."""
    import webview.pages as pages
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "is_multitenant", lambda: True)
    monkeypatch.setattr(pages.webgate, "is_multitenant", lambda: True)
    app = FastAPI()
    app.include_router(pages.router)
    resp = TestClient(app).get("/api/webgate/doctor")
    assert resp.status_code == 403
    assert "tenant identity" in resp.json()["detail"].lower()
