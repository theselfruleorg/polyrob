"""Preferences page — /preferences + GET/PATCH /api/webgate/preferences (owner-UX P4 T3).

Closes the surface-parity gap the 2026-07-12 UI review flagged: the webview was
the only owner surface that could neither view nor change typed prefs. The
endpoints ride the SAME core seams the CLI/REPL/agent use (``core.prefs``:
``PREF_SCHEMA`` / ``display_effective`` / ``write_preference``), so displayed
state can never drift from enforcement.

Semantics (per docs/plans/2026-07-11-owner-ux-plan-phase4-surface-parity.md T3):
- GET: schema-driven — every PREF_SCHEMA key with type/sensitivity/applies/
  description + effective value & source. Tenant via ``_effective_user_id``.
- PATCH {key, value, confirm?}: SAFE keys write immediately; GUARDED keys
  without ``confirm:true`` → 409 {guarded: true} (UI shows a confirm modal);
  with ``confirm:true`` → applied directly (the PATCH IS the explicit owner
  confirmation — same trust level as `polyrob config set --confirm`).
- WEBVIEW_READ_ONLY → PATCH 403, GET allowed.
"""
import importlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _router_client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


# --------------------------------------------------------------------------- #
# GET — schema-driven, effective value + source
# --------------------------------------------------------------------------- #

def test_get_lists_every_schema_key_with_spec_fields(monkeypatch, tmp_path):
    from core.prefs import PREF_SCHEMA
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.get("/api/webgate/preferences")
    assert r.status_code == 200
    body = r.json()
    items = {it["key"]: it for it in body["preferences"]}
    assert set(items) == set(PREF_SCHEMA)
    sample = items["style.verbosity"]
    for field in ("key", "description", "type", "sensitivity", "applies",
                  "value", "source"):
        assert field in sample
    assert sample["sensitivity"] == "safe"
    assert items["approvals.require"]["sensitivity"] == "guarded"
    # enum keys surface their allowed values so the UI can render a select
    assert "terse" in items["style.verbosity"]["enum_values"]


def test_get_shows_written_pref_as_effective(monkeypatch, tmp_path):
    from core.prefs import write_preference
    client, _pages = _router_client(monkeypatch, tmp_path)
    ok, err = write_preference(tmp_path, "u1", "style.verbosity", "terse")
    assert ok, err
    items = {it["key"]: it for it in client.get("/api/webgate/preferences").json()["preferences"]}
    assert items["style.verbosity"]["value"] == "terse"
    assert items["style.verbosity"]["source"] == "pref"


# --------------------------------------------------------------------------- #
# PATCH — safe / guarded / invalid
# --------------------------------------------------------------------------- #

def test_patch_safe_key_writes_immediately(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "style.verbosity", "value": "terse"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["applies"] == "next-turn"
    assert load_preferences(tmp_path, "u1").get("style.verbosity") == "terse"


def test_patch_guarded_key_without_confirm_409(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "budget.wallet_daily_usd", "value": 2.5})
    assert r.status_code == 409
    assert r.json()["guarded"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") is None


def test_patch_guarded_key_with_confirm_applies(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "budget.wallet_daily_usd", "value": 2.5,
                           "confirm": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") == 2.5


def test_patch_unknown_key_400(monkeypatch, tmp_path):
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "not.a.pref", "value": "x"})
    assert r.status_code == 400


def test_patch_invalid_value_400_with_error(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "style.verbosity", "value": "shouty"})
    assert r.status_code == 400
    assert r.json()["error"]
    assert load_preferences(tmp_path, "u1").get("style.verbosity") is None


# --------------------------------------------------------------------------- #
# Review fixes (owner-UX P4 T3 review): strict boolean confirm, 400 on
# malformed body / missing value — reproduced live by the reviewer.
# --------------------------------------------------------------------------- #

def test_patch_guarded_key_string_false_confirm_is_not_true_409(monkeypatch, tmp_path):
    """Reviewer's exact repro: confirm:"false" (a truthy non-empty string) must
    NOT be accepted as confirmation — the guarded write must be refused."""
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "budget.wallet_daily_usd", "value": 2.5,
                           "confirm": "false"})
    assert r.status_code == 409
    assert r.json()["guarded"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") is None


def test_patch_guarded_key_truthy_non_bool_confirm_rejected(monkeypatch, tmp_path):
    """confirm:1 (truthy, but not a literal JSON boolean) must also be refused;
    only confirm:true (an actual bool) may apply a guarded write."""
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "budget.wallet_daily_usd", "value": 2.5,
                           "confirm": 1})
    assert r.status_code == 409
    assert r.json()["guarded"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") is None

    r2 = client.patch("/api/webgate/preferences",
                      json={"key": "budget.wallet_daily_usd", "value": 2.5,
                            "confirm": True})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") == 2.5


def test_patch_non_dict_body_list_400_not_500(monkeypatch, tmp_path):
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences", json=[1, 2, 3])
    assert r.status_code == 400


def test_patch_non_dict_body_string_400_not_500(monkeypatch, tmp_path):
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences", json="x")
    assert r.status_code == 400


def test_patch_missing_value_400_nothing_written(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "session.toolset"})
    assert r.status_code == 400
    assert load_preferences(tmp_path, "u1").get("session.toolset") is None


def test_patch_guarded_key_missing_value_400_nothing_written(monkeypatch, tmp_path):
    """Missing ``value`` must be rejected BEFORE the guarded-confirm check
    applies any write — including on a guarded key with a bogus confirm."""
    from core.prefs import load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "approvals.deny", "confirm": "false"})
    assert r.status_code == 400
    assert load_preferences(tmp_path, "u1").get("approvals.deny") is None


# --------------------------------------------------------------------------- #
# List-shrink routes through review (owner-UX P2-4 final review, item 3):
# in local posture the webview has no auth, so a confirmed wholesale-replace
# of a GUARDED list key could drop a pref-added gate — bypassing the
# remove_entry owner-review flow every other surface (``/approve remove``,
# the agent-callable ``preferences`` action) enforces. A removed entry is
# queued for review instead of applied; additions in the same PATCH still
# apply directly (tightening).
# --------------------------------------------------------------------------- #

def test_patch_list_removal_queued_for_review_202(monkeypatch, tmp_path):
    from core.prefs import list_pending_pref_changes, load_preferences, write_preference
    client, _pages = _router_client(monkeypatch, tmp_path)
    ok, err = write_preference(tmp_path, "u1", "approvals.require", ["x402_request", "git_push"])
    assert ok, err

    r = client.patch("/api/webgate/preferences",
                     json={"key": "approvals.require", "value": ["x402_request"],
                           "confirm": True})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] == [
        {"key": "approvals.require", "entry": "git_push", "proposal_id": "approvals.require"}
    ]
    assert body["applied_additions"] == []

    # The removal was NOT applied — the entry is still effective.
    current = load_preferences(tmp_path, "u1").get("approvals.require")
    assert "git_push" in current and "x402_request" in current

    # A pending proposal exists for the owner to review.
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any(p["id"] == "approvals.require" for p in pending)


def test_patch_list_pure_addition_with_confirm_applies_directly(monkeypatch, tmp_path):
    from core.prefs import list_pending_pref_changes, load_preferences, write_preference
    client, _pages = _router_client(monkeypatch, tmp_path)
    ok, err = write_preference(tmp_path, "u1", "approvals.require", ["x402_request"])
    assert ok, err

    r = client.patch("/api/webgate/preferences",
                     json={"key": "approvals.require",
                           "value": ["x402_request", "git_push"], "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True

    current = load_preferences(tmp_path, "u1").get("approvals.require")
    assert set(current) == {"x402_request", "git_push"}
    assert list_pending_pref_changes("u1", tmp_path) == []


def test_patch_scalar_guarded_key_with_confirm_still_applies_directly(monkeypatch, tmp_path):
    """Scalar (non-list) guarded keys are unaffected by the list-shrink review
    split — same 200 direct-apply as before (regression guard)."""
    from core.prefs import list_pending_pref_changes, load_preferences
    client, _pages = _router_client(monkeypatch, tmp_path)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "budget.wallet_daily_usd", "value": 10.0,
                           "confirm": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") == 10.0
    assert list_pending_pref_changes("u1", tmp_path) == []


def test_patch_list_removal_without_confirm_still_409(monkeypatch, tmp_path):
    """The existing guarded 409-without-confirm gate runs BEFORE the
    list-shrink split — unconfirmed requests never reach the diff logic."""
    from core.prefs import load_preferences, write_preference
    client, _pages = _router_client(monkeypatch, tmp_path)
    write_preference(tmp_path, "u1", "approvals.require", ["x402_request", "git_push"])
    r = client.patch("/api/webgate/preferences",
                     json={"key": "approvals.require", "value": ["x402_request"]})
    assert r.status_code == 409
    assert r.json()["guarded"] is True
    assert load_preferences(tmp_path, "u1").get("approvals.require") == \
        ["x402_request", "git_push"]


# --------------------------------------------------------------------------- #
# Auth matrix
# --------------------------------------------------------------------------- #

def test_read_only_blocks_patch_allows_get(monkeypatch, tmp_path):
    client, _pages = _router_client(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    assert client.get("/api/webgate/preferences").status_code == 200
    r = client.patch("/api/webgate/preferences",
                     json={"key": "style.verbosity", "value": "terse"})
    assert r.status_code == 403


def test_patch_respects_tenant_identity_failure(monkeypatch, tmp_path):
    """PATCH must go through the same fail-closed tenant resolution as GET —
    a multitenant request without an authenticated identity is 403, never a
    write into the owner's prefs file."""
    import webview.pages as pages
    client, _pages = _router_client(monkeypatch, tmp_path)

    def _deny(_req):
        raise HTTPException(status_code=403, detail="tenant required")

    monkeypatch.setattr(pages, "_effective_user_id", _deny)
    r = client.patch("/api/webgate/preferences",
                     json={"key": "style.verbosity", "value": "terse"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Page route
# --------------------------------------------------------------------------- #

def test_preferences_page_renders_200(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)
    client = TestClient(server._fastapi)
    r = client.get("/preferences")
    assert r.status_code == 200
    assert "Preferences" in r.text


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    import webview.server as server
    importlib.reload(server)
