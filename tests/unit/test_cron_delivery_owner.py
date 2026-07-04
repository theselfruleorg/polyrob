"""Tests for cron delivery owner-telegram resolution (headless outreach fix)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cron.delivery import _configured_owner_telegram_id, _owner_telegram  # noqa: E402


class _Job:
    def __init__(self, user_id):
        self.user_id = user_id


class _Agent:
    container = None


def test_configured_owner_single_allowed_id(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "28436760")
    assert _configured_owner_telegram_id() == "28436760"


def test_configured_owner_explicit_wins(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "111")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "222")
    assert _configured_owner_telegram_id() == "111"


def test_configured_owner_ambiguous_multi_id_is_none(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,222")
    assert _configured_owner_telegram_id() is None


def test_owner_telegram_falls_back_for_nonnumeric_tenant(monkeypatch):
    """The headline bug: a goal/cron run as user_id='rob' must still resolve the owner."""
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "28436760")
    assert _owner_telegram(_Agent(), _Job("rob")) == "28436760"


def test_owner_telegram_numeric_uid_is_chat(monkeypatch):
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    assert _owner_telegram(_Agent(), _Job("99887766")) == "99887766"


def test_owner_telegram_none_when_unresolvable(monkeypatch):
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    assert _owner_telegram(_Agent(), _Job("rob")) is None


def test_owner_alias_roundtrip_prod_config(monkeypatch):
    """End-to-end (handoff seam 3): an owner aliased to tenant 'rob' at inbound must
    have its OUT-OF-BAND cron/goal/self-wake delivery resolve back to the owner's tg
    chat via the SAME core SSOT. Mirrors prod: explicit POLYROB_OWNER_TELEGRAM_ID.
    """
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    # No user_directory row maps 'rob' -> a tg chat (the alias skips resolve_internal,
    # and the UNIQUE(tg_user_id) constraint forbids upserting one), so delivery MUST
    # fall through to the configured owner id.
    assert _owner_telegram(_Agent(), _Job("rob")) == "28436760"
