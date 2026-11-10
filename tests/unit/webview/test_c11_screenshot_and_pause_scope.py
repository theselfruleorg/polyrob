"""113 W6 item 7 (C-11): the guide promised browser screenshots and a pause by scope
and duration. The Work pane now shows a chat's last browser screenshot from the
existing ``GET /api/session/{id}/screenshot`` reader; the header Pause pauses
everything, and the guide says that a scope or an end time is ``/pause`` in the chat box
(the console verb plane runs it), which is what the console really has."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_the_work_pane_has_the_screenshot_section():
    chat = (ROOT / "webview" / "templates" / "chat.html").read_text()
    aside = chat.split('<aside class="work-pane"', 1)[1].split("</aside>", 1)[0]
    assert 'id="workpane-shot"' in aside
    assert '<script src="/static/app/workpane-shot.js" type="module"></script>' in chat


def test_the_reader_it_uses_exists():
    import webview.server as server
    paths = {getattr(r, "path", "") for r in server._fastapi.routes}
    assert "/api/session/{session_id}/screenshot" in paths
    assert "/api/session/{session_id}/screenshot/file" in paths


def test_the_header_pause_is_everything_and_the_guide_says_where_a_scope_is():
    pause = (ROOT / "webview" / "static" / "app" / "pause.js").read_text()
    assert "{ scopes: ['all'] }" in pause
    guide = (ROOT / "docs" / "guide" / "console.md").read_text()
    assert "pause and resume autonomy, by scope and with a duration;" not in guide
    assert "/pause background for 6h" in guide
