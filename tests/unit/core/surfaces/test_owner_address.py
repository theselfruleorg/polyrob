"""030 WS-B1: the owner-address contract + surface-aware owner delivery.

`deliver_user_message` resolved a TELEGRAM chat id and a TELEGRAM sink by
construction — on a non-Telegram deploy every approval prompt, credit-sentinel
halt and settlement alert became an unread telemetry row. Now:
- `core.surfaces.owner_address` answers "which surface(s), what address";
- unset env keeps the legacy telegram behavior byte-compatible;
- OWNER_SURFACE picks the primary + fallback chain;
- a critical-lane notice broadcasts to every configured surface.
"""
import asyncio
import time

import pytest


class _EvLog:
    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None,
               attrs=None, **kw):
        merged = dict(kw)
        if attrs:
            merged.update(attrs)
        self.events.append({"ts": ts if ts is not None else time.time(), "kind": kind,
                            "user_id": user_id, "session_id": session_id,
                            "source": source, "attrs": merged})

    def query(self, *, since_ts=None, kind=None, user_id=None, limit=500):
        out = [e for e in self.events
               if (kind is None or e["kind"] == kind)
               and (user_id is None or e["user_id"] == user_id)
               and (since_ts is None or e["ts"] >= since_ts)]
        return sorted(out, key=lambda e: -e["ts"])[:limit]


class _Router:
    """message_router stand-in recording (chat_id, text, surface_id)."""

    def __init__(self, fail_surfaces=()):
        self.sent = []
        self._fail = set(fail_surfaces)

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        if surface_id in self._fail:
            return False
        self.sent.append((surface_id, chat_id, text))
        return True


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


def _deliver(container, user_id, text, **kw):
    from core.surfaces.user_delivery import deliver_user_message
    return asyncio.run(deliver_user_message(container, user_id, text, **kw))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OWNER_SURFACE", "OWNER_SURFACES", "OWNER_SLACK_ID",
              "OWNER_DISCORD_ID", "OWNER_SIGNAL_ID", "OWNER_WHATSAPP_ID",
              "OWNER_X_ID", "POLYROB_OWNER_EMAIL", "BOT_OWNER_EMAIL"):
        monkeypatch.delenv(k, raising=False)


# --- owner_address ----------------------------------------------------------

def test_owner_address_telegram_matches_legacy_resolver():
    from core.surfaces.owner_address import owner_address
    c = _Container({})
    assert owner_address(c, "telegram", "12345") == "12345"  # digit-uid rule


def test_owner_address_env_per_surface(monkeypatch):
    from core.surfaces.owner_address import owner_address
    monkeypatch.setenv("OWNER_SLACK_ID", "C0FFEE")
    assert owner_address(_Container({}), "slack", "u1") == "C0FFEE"
    assert owner_address(_Container({}), "discord", "u1") is None


def test_owner_surface_order_default_and_env(monkeypatch):
    from core.surfaces.owner_address import owner_surface_order
    assert owner_surface_order() == ["telegram"]
    monkeypatch.setenv("OWNER_SURFACE", "slack, telegram")
    assert owner_surface_order() == ["slack", "telegram"]


# --- surface-aware delivery -------------------------------------------------

def test_default_env_keeps_legacy_telegram_path():
    router, ev = _Router(), _EvLog()
    c = _Container({"message_router": router})
    out = _deliver(c, "12345", "progress note", event_log=ev)
    assert out == "sent"
    assert router.sent == [("telegram", "12345", "progress note")]


def test_owner_surface_routes_to_the_configured_surface(monkeypatch):
    monkeypatch.setenv("OWNER_SURFACE", "slack")
    monkeypatch.setenv("OWNER_SLACK_ID", "C0FFEE")
    router, ev = _Router(), _EvLog()
    c = _Container({"message_router": router})
    out = _deliver(c, "u1", "approval needed for self_env_install_dep", event_log=ev)
    assert out == "sent"
    assert router.sent == [("slack", "C0FFEE", "approval needed for self_env_install_dep")]


def test_fallback_chain_delivers_on_the_second_surface(monkeypatch):
    monkeypatch.setenv("OWNER_SURFACE", "slack,telegram")
    monkeypatch.setenv("OWNER_SLACK_ID", "C0FFEE")
    router, ev = _Router(fail_surfaces={"slack"}), _EvLog()
    c = _Container({"message_router": router})
    out = _deliver(c, "12345", "goal blocked: needs a grant", event_log=ev)
    assert out == "sent"
    assert router.sent == [("telegram", "12345", "goal blocked: needs a grant")]


def test_critical_source_broadcasts_to_every_configured_surface(monkeypatch):
    monkeypatch.setenv("OWNER_SURFACE", "slack,telegram")
    monkeypatch.setenv("OWNER_SLACK_ID", "C0FFEE")
    router, ev = _Router(), _EvLog()
    c = _Container({"message_router": router})
    out = _deliver(c, "12345", "credit death: provider exhausted",
                   source="credit_sentinel", event_log=ev)
    assert out == "sent"
    surfaces = {s for s, _, _ in router.sent}
    assert surfaces == {"slack", "telegram"}


def test_no_reachable_surface_still_falls_back_durably(monkeypatch):
    monkeypatch.setenv("OWNER_SURFACE", "slack")
    # no OWNER_SLACK_ID -> no address -> durable fallback, never a crash
    router, ev = _Router(), _EvLog()
    c = _Container({"message_router": router})
    out = _deliver(c, "u-nondigit", "hello", event_log=ev)
    assert out == "fallback"
    assert router.sent == []
    assert any(e["kind"] == "owner_notice" for e in ev.events)
