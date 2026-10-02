"""X evaluation 2026-09-26, build item 4: the X OAuth 2.0 login (the DM rail)
on `/status` and `polyrob doctor`.

On 09-25 the OAuth 2.0 user token died (refresh token revoked by a client-secret
regen) and DMs were down for a day while every seat read healthy: the X lines
read only the OAuth 1.0a env keys and the browser session row. The pack's
refresher now records an ``x_oauth2`` credential verdict when the login dies;
these tests pin how the core tier renders it — without importing the pack and
without decrypting the token store.
"""
import json

from core import credential_verdicts as cv
from core import status_snapshot as ss


def _store(tmp_path, keys):
    (tmp_path / ".x_session.json").write_text(json.dumps({k: "gAAAAopaque" for k in keys}))


def test_an_open_verdict_says_relogin_needed_with_its_since_and_remedy(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    _store(tmp_path, ["rob|x_oauth2"])  # a record exists, but the login is dead
    cv.record_rejection("x_oauth2", "", code="relogin_needed",
                        remedy="/x login (or `polyrob x-account oauth-login`)")
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "relogin_needed"
    assert line.startswith("X login (OAuth 2.0, DMs): re-login needed since ")
    assert "/x login (or `polyrob x-account oauth-login`)" in line


def test_the_verdict_without_a_remedy_still_names_the_command(tmp_path):
    cv.record_rejection("x_oauth2", "", code="relogin_needed")
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "relogin_needed"
    assert ss.X_OAUTH2_RELOGIN_REMEDY in line


def test_a_stored_login_with_no_verdict_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    _store(tmp_path, ["rob|x", "rob|x_oauth2"])
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "stored"
    assert "re-login" not in line


def test_a_browser_only_store_is_not_an_oauth2_login(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    _store(tmp_path, ["rob|x"])
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "none"
    assert "none" in line


def test_an_unreadable_store_is_not_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    (tmp_path / ".x_session.json").write_text("{not json")
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "unreadable"
    assert "unreadable" in line


def test_a_static_env_token_is_named_as_short_lived(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "t")
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "env_token"
    assert "~2h" in line


def test_the_identity_section_carries_the_line(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    cv.record_rejection("x_oauth2", "", code="relogin_needed")
    sec = ss._identity_section("rob", str(tmp_path))
    assert sec.data["x_oauth2"] == "relogin_needed"
    assert any(l.startswith("X login (OAuth 2.0, DMs): re-login needed") for l in sec.lines)


def test_the_verdict_is_a_warn_health_item_with_the_remedy():
    cv.record_rejection("x_oauth2", "", code="relogin_needed", remedy="/x login")
    [v] = [v for v in ss._rail_verdicts() if v.kind == "x_oauth2"]
    text, sev, key = ss._rail_verdict_line(v)
    assert sev == ss.SEVERITY_WARN
    assert key == "x_oauth2_relogin"
    assert "re-login needed since" in text and "/x login" in text


def test_a_cleared_verdict_leaves_no_line(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    _store(tmp_path, ["rob|x_oauth2"])
    cv.record_rejection("x_oauth2", "", code="relogin_needed")
    cv.clear_rejection("x_oauth2", "")
    _, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "stored"
    assert not [v for v in ss._rail_verdicts() if v.kind == "x_oauth2"]


def test_a_store_written_after_the_refusal_is_named(tmp_path, monkeypatch):
    """The pack treats a record newer than the verdict as a re-login it could not
    clear; core cannot decrypt obtained_at, so it names the store change."""
    import os
    import time
    cv.record_rejection("x_oauth2", "", code="relogin_needed")
    _store(tmp_path, ["rob|x_oauth2"])
    later = time.time() + 5
    os.utime(tmp_path / ".x_session.json", (later, later))
    line, state = ss.x_oauth2_line(str(tmp_path))
    assert state == "relogin_needed"
    assert "the token store changed since" in line
