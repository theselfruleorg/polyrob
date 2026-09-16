"""A filesystem path is not an address (design C4 / plan T5).

The console deep-link helpers existed but were called from exactly two places,
both framework-side goal completions. Every agent-authored message —
send_message, done, the `message` tool — could only emit `/var/lib/...`, which
the owner cannot open from a phone.
"""
import os

import pytest

from core.surfaces import path_links


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    (d / "report.md").write_text("# Q3\n" + "body\n" * 10)
    return d


def _resolve(text, ws, **kw):
    kw.setdefault("session_id", "sess1")
    kw.setdefault("workspace_dir", str(ws))
    kw.setdefault("media_ok", False)
    return path_links.resolve_paths(text, **kw)


# --- precedence: attach > link > honest ------------------------------------

def test_a_media_capable_surface_attaches_the_file(ws):
    r = _resolve(f"Report saved to {ws/'report.md'}", ws, media_ok=True)
    assert [e["path"] for e in r.attachments] == [str(ws / "report.md")]
    assert r.resolved == [(str(ws / "report.md"), "attached")]
    assert str(ws / "report.md") not in r.text
    assert "report.md" in r.text


def test_without_media_the_path_becomes_a_console_url(ws, monkeypatch):
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", "https://console.example.com")
    r = _resolve(f"Report saved to {ws/'report.md'}", ws)
    assert "https://console.example.com/api/session/sess1/workspace/serve/report.md" in r.text
    assert r.attachments == []


def test_with_no_console_and_no_media_the_message_is_honest(ws):
    r = _resolve(f"Report saved to {ws/'report.md'}", ws)
    assert "server-only" in r.text
    assert "report.md" in r.text
    # The unreachable absolute path is never presented as if it were an address.
    assert str(ws / "report.md") not in r.text


# --- confinement ------------------------------------------------------------

def test_a_path_outside_the_workspace_is_left_alone(ws):
    r = _resolve("See /etc/polyrob/polyrob.env for the setting", ws, media_ok=True)
    assert r.text == "See /etc/polyrob/polyrob.env for the setting"
    assert r.attachments == [] and r.resolved == []


def test_a_symlink_escape_is_not_attached(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = ws / "link.txt"
    os.symlink(outside, link)
    r = _resolve(f"See {link}", ws, media_ok=True)
    assert r.attachments == []


# --- screening --------------------------------------------------------------

def test_an_oversized_file_is_never_attached(ws, monkeypatch):
    monkeypatch.setenv("DELIVERABLES_ATTACH_MAX_MB", "0.000001")
    r = _resolve(f"Report saved to {ws/'report.md'}", ws, media_ok=True)
    assert r.attachments == []
    assert "server-only" in r.text
    assert "exceeds" in r.text


def test_a_secret_shaped_file_is_never_attached(ws):
    (ws / ".env").write_text("OPENAI_API_KEY=sk-x")
    r = _resolve(f"Config at {ws/'.env'}", ws, media_ok=True)
    assert r.attachments == []
    assert "server-only" in r.text


# --- bounds and gating ------------------------------------------------------

def test_the_attachment_count_is_capped(ws, monkeypatch):
    monkeypatch.setenv("DELIVERABLES_ATTACH_MAX_FILES", "1")
    for n in ("a.md", "b.md"):
        (ws / n).write_text("x" * 20)
    r = _resolve(f"{ws/'a.md'} and {ws/'b.md'}", ws, media_ok=True)
    assert len(r.attachments) == 1
    assert sum(1 for _, how in r.resolved if how == "attached") == 1


def test_the_flag_off_leaves_the_text_untouched(ws, monkeypatch):
    monkeypatch.setenv("CHAT_PATH_LINKS", "off")
    original = f"Report saved to {ws/'report.md'}"
    r = _resolve(original, ws, media_ok=True)
    assert r.text == original and r.attachments == []


def test_a_message_with_no_paths_is_returned_unchanged(ws):
    r = _resolve("All done, nothing to attach.", ws, media_ok=True)
    assert r.text == "All done, nothing to attach."
    assert r.attachments == [] and r.resolved == []


def test_a_missing_workspace_is_a_no_op(ws):
    original = f"Report saved to {ws/'report.md'}"
    r = _resolve(original, ws, workspace_dir=None, media_ok=True)
    assert r.text == original and r.attachments == []


def test_a_nonexistent_file_inside_the_workspace_is_reported_honestly(ws):
    r = _resolve(f"See {ws/'ghost.md'}", ws, media_ok=True)
    assert r.attachments == []
    assert "server-only" in r.text
