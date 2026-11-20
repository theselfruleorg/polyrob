"""The chat seats' command catalogue and the lifecycle gate live below the seats
(proposal 125 S14): core/surfaces/command_catalog.py and lifecycle_verbs.py import no
seat, and the Telegram harness and the console read the same objects."""
import ast
import asyncio
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]


def _imports(rel: str) -> set:
    tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_the_moved_modules_import_no_seat():
    for rel in ("core/surfaces/command_catalog.py", "core/surfaces/lifecycle_verbs.py"):
        bad = sorted(m for m in _imports(rel)
                     if m.split(".")[0] in ("surfaces", "cli", "webview", "api"))
        assert not bad, f"{rel} imports a seat: {bad}"


def test_the_harness_names_are_the_core_objects():
    import core.surfaces.command_catalog as cc
    import core.surfaces.lifecycle_verbs as lv
    import surfaces.telegram.harness as h
    assert h._help_text is cc.help_text and h._USAGE is cc.USAGE
    assert h._HELP_BODY == cc.help_body()
    assert h._unknown_command_text is cc.unknown_text
    assert h._lifecycle_permitted is lv.lifecycle_permitted
    assert h.CANCEL_REACH == lv.CANCEL_REACH


def test_the_console_seam_reads_core():
    import core.surfaces.command_catalog as cc
    from surfaces import _actor
    assert _actor.help_text() == cc.help_text()
    assert _actor.unknown_command_text("/mutee") == cc.unknown_text("/mutee")
    from core.surfaces.room_grants import ADMIN_ELSEWHERE_TEXT
    assert cc.unknown_text("/admin") == ADMIN_ELSEWHERE_TEXT


def test_cancel_names_its_reach_and_never_claims_a_stop_it_did_not_make(monkeypatch):
    import core.surfaces.lifecycle_verbs as lv
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from core.surfaces.act import InboundResult
    monkeypatch.setattr(lv, "_is_owner", lambda uid: True)

    class _Agent:
        def __init__(self, stopped):
            self.stopped = stopped

        async def cancel_session_by_id(self, sid, force=False):
            return self.stopped

    src = SessionSource("telegram", "1", "dm")
    res = InboundResult(
        inbound=InboundMessage(text="/cancel", identity=Identity(user_id="u", source=src)),
        decision=RouteDecision(RouteKind.COMMAND, "k", session_id="s1", command="/cancel"))
    assert asyncio.run(lv.cancel_reply(_Agent(True), res)) == f"Stopped. {lv.CANCEL_REACH}"
    assert asyncio.run(lv.cancel_reply(_Agent(False), res)).startswith("Nothing was running")
