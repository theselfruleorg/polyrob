"""X evaluation 2026-09-26 item 4: the x pack's status lines (secret-free)."""
import pytest

pytest.importorskip("polyrob_x")

from polyrob_x import status as xs  # noqa: E402


def test_relogin_needed_names_since_and_remedy():
    [line] = xs.lines_from({"stored": True, "relogin_needed": True,
                            "relogin_needed_since": "09-25 19:40Z (4d)",
                            "relogin_remedy": "/x login"})
    assert line == ("X login (OAuth 2.0, DMs): re-login needed since 09-25 19:40Z (4d) "
                    "— /x login")


def test_valid_login_shows_expiry_and_scope():
    out = xs.lines_from({"stored": True, "expires_in_sec": 1800, "has_refresh_token": True,
                         "client_id_set": True, "scope": "dm.read tweet.read"})
    assert out[0] == "X login (OAuth 2.0, DMs): valid, access token expires in 30m"
    assert "X login scope: dm.read tweet.read" in out


def test_expired_access_with_refresh_is_not_an_alarm():
    out = xs.lines_from({"stored": True, "expires_in_sec": -120, "has_refresh_token": True,
                         "client_id_set": True})
    assert "the next call refreshes it" in out[0]


def test_missing_client_id_is_named():
    out = xs.lines_from({"stored": True, "expires_in_sec": 60, "has_refresh_token": True,
                         "client_id_set": False})
    assert any("TWITTER_OAUTH2_CLIENT_ID" in l for l in out)


def test_no_login_and_env_only():
    assert "none" in xs.lines_from({"stored": False})[0]
    assert "static env token" in xs.lines_from({"stored": False, "static_env": True})[0]


def test_build_never_leaks_a_token_and_never_raises(monkeypatch):
    import polyrob_x.x_oauth2 as xo
    monkeypatch.setattr(xo, "status", lambda: {"stored": True, "expires_in_sec": 100,
                                               "has_refresh_token": True,
                                               "client_id_set": True})
    assert xs.build()[0].startswith("X login (OAuth 2.0, DMs): valid")

    def boom():
        raise PermissionError("EACCES")
    monkeypatch.setattr(xo, "status", boom)
    [line] = xs.build()
    assert "unreadable (PermissionError" in line
