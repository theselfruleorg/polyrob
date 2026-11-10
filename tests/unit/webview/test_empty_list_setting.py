"""An EMPTY list setting is an empty list on Agent › Settings, never the word "empty".

``core.prefs.display_effective`` takes an unset list setting's built-in default from
the flags catalog, whose display word for an empty list is ``"empty"``. The console
showed that word as the value and, on Save, would have stored ``"empty"`` as an
entry. ``webview/prefs_view.py`` sends an empty list instead (``core/prefs.py`` is at
its size ceiling, so the fix is on the console side).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.prefs import PREF_SCHEMA, display_effective as core_display_effective


def _client(monkeypatch, tmp_path):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def _empty_word_keys(tmp_path):
    return [k for k, s in PREF_SCHEMA.items()
            if s.type == "list" and core_display_effective(k, "u1", tmp_path)[0] == "empty"]


def test_the_core_display_still_says_empty_for_an_unset_list(tmp_path):
    # The reproduction: without the console fix the reader sends this word.
    assert _empty_word_keys(tmp_path), "no list setting shows the catalog word any more"


def test_the_console_sends_an_empty_list(monkeypatch, tmp_path):
    keys = _empty_word_keys(tmp_path)
    body = _client(monkeypatch, tmp_path).get("/api/webgate/preferences").json()
    items = {it["key"]: it for it in body["preferences"]}
    for key in keys:
        assert items[key]["value"] == [], (key, items[key]["value"])
    for key, spec in PREF_SCHEMA.items():
        if spec.type == "list":
            assert items[key]["value"] != "empty", key


def test_a_written_list_is_unchanged(monkeypatch, tmp_path):
    from webview.prefs_view import display_effective
    from core.prefs import write_preference
    ok, _ = write_preference(tmp_path, "u1", "approvals.require", ["send"])
    assert ok
    assert display_effective("approvals.require", "u1", tmp_path)[0] == ["send"]


def test_a_scalar_setting_is_unchanged(tmp_path):
    from webview.prefs_view import display_effective
    assert display_effective("ui.theme", "u1", tmp_path) == core_display_effective(
        "ui.theme", "u1", tmp_path)
