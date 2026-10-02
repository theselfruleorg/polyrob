"""``/x status`` must name what each X credential unlocks and how to fix it.

2026-10-02: the owner ran ``/x login`` (OAuth 2.0, the DM half) expecting it to
unblock outreach. OUTREACH kept skipping with "no X session stored", because
that is the OTHER credential: the browser session, whose remedy is
``polyrob x-account capture-session``. The status read named both credentials
but said neither what each is for nor how to restore the browser one.
"""
from polyrob_x.owner_verbs import status_lines

_OAUTH_OK = {"stored": True, "expired": False, "expires_in_sec": 7200,
             "has_refresh_token": True, "scope": "dm.read dm.write", "client_id_set": True}


def _snap(oauth2, browser):
    return {"oauth2": oauth2, "verdict": None, "browser": browser,
            "login_link": {"ready": True}}


def test_missing_browser_session_names_capture_session_and_what_it_unlocks():
    text = "\n".join(status_lines(_snap(_OAUTH_OK, {"stored": False})))
    line = next(l for l in text.splitlines() if l.startswith("• Browser session"))
    assert "none stored" in line
    assert "posts" in line and "outreach" in line
    assert "polyrob x-account capture-session" in text


def test_api_login_line_says_it_covers_dms():
    text = "\n".join(status_lines(_snap(_OAUTH_OK, {"stored": True, "handle": "rob"})))
    line = next(l for l in text.splitlines() if l.startswith("• API login"))
    assert "DMs" in line


def test_stored_browser_session_does_not_print_the_capture_remedy():
    text = "\n".join(status_lines(_snap(_OAUTH_OK, {"stored": True, "handle": "rob"})))
    assert "capture-session" not in text
