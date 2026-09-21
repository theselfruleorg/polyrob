"""C5 (2026-09-15 prod review): an owner-tier `message`-tool send is BOOKED on
the delivery rail's ledger.

`perform_message_send` never touched `core.surfaces.user_delivery`, so a send
to the owner through the `message` tool was invisible to the shared daily cap
and hourly rate limit every OTHER owner-bound producer is measured against.
Two rails, one owner, no shared accounting.
"""
import asyncio

import pytest


class _Router:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    def capabilities(self, surface_id):
        return None

    def bot_username(self, surface_id):
        return None

    async def send_message(self, chat_id, text, surface_id=None, media=None):
        self.sent.append((surface_id, chat_id, text))
        return self._ok


class _Container:
    def __init__(self, services=None):
        self._s = services or {}
        self.config = None

    def get_service(self, name):
        return self._s.get(name)


def _send(**kw):
    from tools.controller.message_send import perform_message_send
    base = dict(router=_Router(), allowlist=None,
                owner_targets={"telegram": "28436760"}, user_id="rob",
                surface="telegram", target="owner", text="the owner report",
                container=_Container())
    base.update(kw)
    return asyncio.run(perform_message_send(**base))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    import core.event_log as el
    monkeypatch.setattr(el, "_LOG", None, raising=False)
    return tmp_path


def _rail_rows(tmp_path):
    from core.event_log import TelemetryEventLog
    return TelemetryEventLog(str(tmp_path / "t.db")).query(kind="user_delivery",
                                                           user_id="rob")


def test_owner_send_is_recorded_on_the_delivery_rail(tmp_path):
    res = _send()
    assert res["success"] is True
    rows = _rail_rows(tmp_path)
    assert rows, "an owner-tier message-tool send must land in the rail's ledger"
    attrs = rows[0]["attrs"]
    assert attrs["outcome"] == "sent"
    # D20 (2026-09-21): the row is BOOKED so the window stays honest about what
    # the owner received, but it rides the `exempt` lane — this path is gated by
    # its own 2h cooldown, not by the daily cap, so it must not be able to spend
    # a budget it cannot be denied for.
    assert attrs["lane"] == "exempt"
    assert attrs["text"] == "the owner report"
    assert rows[0]["source"] == "message_tool"


def test_a_failed_owner_send_is_not_booked(tmp_path):
    res = _send(router=_Router(ok=False))
    assert res["success"] is False
    assert _rail_rows(tmp_path) == []


# --- D20 / D24 (2026-09-21 interface audit) --------------------------------

def test_the_booked_row_rides_the_exempt_lane(tmp_path):
    """D20: this path is gated by its OWN 2h cooldown, not by the daily cap, so
    an unbounded producer was spending a budget only OTHER producers can be
    denied for — the same shape C1 removed for the critical lane."""
    from core.surfaces.user_delivery import PRIORITY_EXEMPT, _budgeted

    res = _send()
    assert res["success"] is True
    rows = _rail_rows(tmp_path)
    assert rows, "the owner send was not booked at all"
    assert rows[-1]["attrs"]["lane"] == PRIORITY_EXEMPT
    # …and it is dropped from the counted window.
    assert _budgeted(rows) == []


class _Orch:
    pass


class _Controller:
    def __init__(self, orch):
        self.orchestrator = orch


def test_an_owner_send_records_the_turn_reply(tmp_path):
    """D24: the `message` tool never marked the turn, so an UNBOUND seat (raw
    API, chat_once, /v1) fell back to scanning history, found done()'s
    "✅ Task Complete\\n\\n<recap>" as the last AIMessage, and returned a
    third-person recap as the answer."""
    from core.surfaces.turn_reply import last_reply_text

    orch = _Orch()
    res = _send(controller=_Controller(orch))
    assert res["success"] is True
    assert last_reply_text(orch) == "the owner report"


def test_a_non_owner_send_does_not_claim_the_turn(tmp_path, monkeypatch):
    """A post to a third party is not the answer the USER reads."""
    from core.surfaces.turn_reply import last_reply_text

    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    orch = _Orch()
    res = _send(controller=_Controller(orch), target="them@acme.com",
                surface="email", owner_targets={"email": "owner@example.com"})
    assert res.get("tier") != "owner"
    assert last_reply_text(orch) is None
