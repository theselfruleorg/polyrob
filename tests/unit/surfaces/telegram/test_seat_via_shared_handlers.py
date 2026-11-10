"""113 W4 item 8 (F-10): a shared handler names the seat the line arrived on.

``surfaces.telegram.admin_verbs`` is the owner verb table of every chat seat
(Telegram, the other chat apps, the console chat box). A literal "telegram"
there writes a false seat into the job and mandate audit and picks the wrong
level rules for `/mode`. ``status_ops._seat_via`` is the one reader.
"""
import asyncio
import inspect

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


def _call(cmd, args, surface_id):
    from surfaces.telegram.admin_verbs import AdminCall
    source = SessionSource(surface_id=surface_id, chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=" ".join([cmd, *args]),
                             identity=Identity(user_id="u1", source=source))
    decision = type("D", (), {"session_key": "agent:main:x:dm:1:u1", "session_id": "s1"})()
    result = type("R", (), {"inbound": inbound, "decision": decision})()
    return AdminCall(task_agent=None, result=result, cmd=cmd, user_id="u1",
                     data_dir="/nowhere", instance_id="i1", args=list(args), board=None)


def test_job_writes_name_the_arrival_seat(monkeypatch):
    from surfaces.telegram import admin_verbs, job_ops
    seen = {}
    monkeypatch.setattr(job_ops, "jobs_reply",
                        lambda uid, home, args, *, via: seen.setdefault("via", via))
    asyncio.run(admin_verbs._job(_call("/job", ["pause", "j1"], "discord")))
    assert seen["via"] == "discord"


def test_trade_writes_name_the_arrival_seat(monkeypatch):
    from surfaces.telegram import admin_verbs, trade_ops
    seen = {}
    monkeypatch.setattr(trade_ops, "trade_reply",
                        lambda uid, home, args, *, via: seen.setdefault("via", via))
    asyncio.run(admin_verbs._trade(_call("/trade", ["status"], "webview")))
    assert seen["via"] == "console"


def test_mode_level_change_uses_the_arrival_seat(monkeypatch):
    from surfaces.telegram import admin_verbs
    seen = {}
    monkeypatch.setattr("core.permission_levels.level_reply",
                        lambda name, *, surface, user_id: seen.setdefault("surface", surface))
    from core.permission_levels import LEVELS
    level = next(iter(LEVELS))
    asyncio.run(admin_verbs._mode(_call("/mode", [level], "slack")))
    assert seen["surface"] == "slack"


def test_no_literal_telegram_seat_in_the_shared_table():
    from surfaces.telegram import admin_verbs
    src = inspect.getsource(admin_verbs)
    assert 'via="telegram"' not in src
    assert '"telegram")' not in src
