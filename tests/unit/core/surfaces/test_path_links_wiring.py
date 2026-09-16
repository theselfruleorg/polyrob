"""The path→address rule reaches the agent's OWN emits (design C4/C5, plan T6).

`goals/deliverables.py` already proved the rule on the goal-completion path.
These tests pin it on `send_message` / `done` — the verbs the agent uses when it
talks to the owner directly, which could only ever emit `/var/lib/...`.
"""
import pytest


class _Router:
    def __init__(self):
        self.published = []

    async def publish(self, msg):
        self.published.append(msg)


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    (d / "q3.md").write_text("# Q3 report\n" + "line\n" * 20)
    return d


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")


@pytest.mark.asyncio
async def test_a_named_workspace_file_is_attached_not_pasted(ws):
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    publish = build_discrete_publish(router, "telegram:1", session_id="s1",
                                     workspace_dir=str(ws), media_ok=True)
    await publish(f"Report ready: {ws/'q3.md'}")
    msg = router.published[0]
    assert [e["path"] for e in msg.media] == [str(ws / "q3.md")]
    assert str(ws / "q3.md") not in msg.text
    assert "q3.md" in msg.text


@pytest.mark.asyncio
async def test_without_media_it_links_to_the_console(ws, monkeypatch):
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", "https://console.example.com")
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    publish = build_discrete_publish(router, "telegram:1", session_id="s1",
                                     workspace_dir=str(ws), media_ok=False)
    await publish(f"Report ready: {ws/'q3.md'}")
    assert "https://console.example.com/api/session/s1/workspace/serve/q3.md" \
        in router.published[0].text


@pytest.mark.asyncio
async def test_the_legacy_call_shape_still_works(ws):
    """No session/workspace passed ⇒ byte-identical to the pre-C4 mirror."""
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    original = f"Report ready: {ws/'q3.md'}"
    await build_discrete_publish(router, "telegram:1")(original)
    assert router.published[0].text == original
    assert router.published[0].media == []


@pytest.mark.asyncio
async def test_a_resolver_fault_never_costs_the_message(ws, monkeypatch):
    import core.surfaces.path_links as pl
    monkeypatch.setattr(pl, "resolve_paths",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    original = f"Report ready: {ws/'q3.md'}"
    await build_discrete_publish(router, "telegram:1", session_id="s1",
                                 workspace_dir=str(ws), media_ok=True)(original)
    assert router.published[0].text == original


def test_the_receipt_names_what_actually_happened(ws):
    from core.surfaces.path_links import receipt, resolve_paths
    r = resolve_paths(f"see {ws/'q3.md'}", session_id="s1",
                      workspace_dir=str(ws), media_ok=True)
    assert receipt(r) == "[attached 1 file(s): q3.md]"


def test_the_receipt_is_none_when_no_file_was_named(ws):
    from core.surfaces.path_links import receipt, resolve_paths
    r = resolve_paths("nothing to see", session_id="s1",
                      workspace_dir=str(ws), media_ok=True)
    assert receipt(r) is None
