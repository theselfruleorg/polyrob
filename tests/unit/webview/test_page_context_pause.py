"""043 W11/N11 — every page says whether autonomy is paused, and whether you can act.

`_page_context` carried `request`, `is_multitenant`, `version` and `ws_url` — no
pause state and no `read_only`. Six pages then hand-set `read_only` themselves
and exactly ONE (`/autonomy`) showed the pause state, so a paused agent looked
identical to a running one from `/finance`, `/apps`, `/knowledge`, `/pending`,
`/config`, `/positions`, `/memory`, `/identity` and `/system` — nine surfaces
where the operator can be reading numbers that stopped moving hours ago.

The headline is the SAME text every other seat renders
(`core.status_render.pause_headline_from` over the ONE 031 pause record), so the
console cannot state a pause the runtime does not enforce, or miss one it does.
"""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

#: Every HTML page route in the console's own routers.
#:
#: ⚠️ EMPTY since A21 (2026-09-21) deleted ``/pending``, the last one. The five
#: destinations render their own head truth through ``pages_new`` (pinned in
#: ``test_live_reader``), so the two facts below are asserted on
#: ``_page_context`` directly — it remains the contract any future
#: ``layout.html`` owner page renders from, and the ratchet at the foot of this
#: module is what stops a page from hand-setting ``read_only`` again.
#: /activity page deleted (043 §9); /autonomy, /finance, /positions, /apps
#: deleted (043 §9 phase 3); /memory, /identity, /system, /preferences,
#: /config, /knowledge deleted (043 §9 phase 4).
PAGE_PATHS = []


@pytest.fixture()
def pages(monkeypatch, tmp_path):
    import webview.pages as module
    monkeypatch.setattr(module, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(module, "_data_dir", lambda: str(tmp_path))
    return module


def _app(pages):
    import webview.activity as activity
    import webview.apps_routes as apps_routes
    import webview.knowledge as knowledge
    app = FastAPI()
    app.include_router(pages.router)
    app.include_router(apps_routes.router)
    app.include_router(knowledge.router)
    app.include_router(activity.router)
    # 043 phase 5: /pending is a normal route on pages.router (the WEBVIEW_UI
    # legacy switch and webview.legacy were removed).
    return TestClient(app)


def test_the_context_carries_both_keys(pages):
    import types
    ctx = pages._page_context(types.SimpleNamespace(state=types.SimpleNamespace()))
    assert "pause_headline" in ctx
    assert "read_only" in ctx


def test_the_headline_is_the_shared_render(pages, monkeypatch):
    """Not a second sentence — the one every seat renders from the 031 record."""
    import types
    from core.status_render import pause_headline_from
    from core.surfaces import owner_admin
    state = owner_admin.pause_state(pages._data_dir())
    # 043 A2: the hint names a control that EXISTS. It used to say "the Resume
    # button" / "the Pause button" on a shell that had neither.
    expected = pause_headline_from(state.to_dict(),
                                   resume_hint=pages.PAUSE_CONTROL_HINT,
                                   pause_hint=pages.PAUSE_CONTROL_HINT)
    ctx = pages._page_context(types.SimpleNamespace(state=types.SimpleNamespace()))
    assert ctx["pause_headline"] == expected


def test_a_failed_pause_read_renders_nothing_not_a_lie(pages, monkeypatch):
    """A headline we could not build must be ABSENT, never a cheerful RUNNING."""
    import types
    monkeypatch.setattr(pages.owner_admin, "pause_state",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    ctx = pages._page_context(types.SimpleNamespace(state=types.SimpleNamespace()))
    assert ctx["pause_headline"] == ""


def test_the_headline_names_a_control_that_exists(pages, monkeypatch, tmp_path):
    """043 A2: an always-visible sentence may not point at a missing button."""
    import types
    from core.surfaces import owner_admin
    owner_admin.pause_autonomy(str(tmp_path), scopes=("all",), reason="test",
                               via="test")
    ctx = pages._page_context(types.SimpleNamespace(state=types.SimpleNamespace()))
    assert "PAUSED" in ctx["pause_headline"]
    assert pages.PAUSE_CONTROL_HINT in ctx["pause_headline"]
    assert "button" not in ctx["pause_headline"].lower()


def test_every_page_shows_the_pause_headline_when_paused(pages, monkeypatch, tmp_path):
    from core.surfaces import owner_admin
    owner_admin.pause_autonomy(str(tmp_path), scopes=("all",), reason="test",
                               via="test")
    if not PAGE_PATHS:
        pytest.skip("no layout.html page has an HTTP route since A21")
    client = _app(pages)
    for path in PAGE_PATHS:
        body = client.get(path).text
        assert "PAUSED" in body, f"{path} does not show the pause state"


def test_every_page_states_the_read_only_posture(pages, monkeypatch):
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    import webview.webgate as wg
    importlib.reload(wg)
    try:
        if not PAGE_PATHS:
            import types
            importlib.reload(pages)
            ctx = pages._page_context(
                types.SimpleNamespace(state=types.SimpleNamespace()))
            assert ctx["read_only"] is True
            return
        client = _app(pages)
        for path in PAGE_PATHS:
            body = client.get(path).text
            assert "read-only" in body.lower(), f"{path} does not state the posture"
    finally:
        monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
        importlib.reload(wg)


def test_no_page_hand_sets_read_only_any_more():
    """Six pages set it themselves on top of the shared context — one concern,
    one place, or the two can disagree.

    Scanned over the WHOLE package, not just pages.py: `activity.py` built its
    own two-key context and was the reason to widen this. `pages_new.py` is the
    new shell, which has its own context builder and is not on this rail."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3] / "webview"
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name == "pages_new.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "read_only"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"read_only re-set outside _page_context at {offenders}"
