from types import SimpleNamespace

import pytest

from core.rate_limit import SlidingWindowLimiter
from core.surfaces import media_access
from core.surfaces.access import AccessTier


@pytest.mark.parametrize("tier,mentioned,allowed", [
    (AccessTier.DENIED, True, False),
    (AccessTier.GROUP_MEMBER, False, False),
    (AccessTier.GROUP_MEMBER, True, True),
    (AccessTier.OWNER, False, True),
])
def test_only_admitted_addressed_senders_can_buy_transcription(monkeypatch, tier, mentioned, allowed):
    monkeypatch.setattr(media_access, "resolve_access_tier", lambda *a: tier)
    monkeypatch.setattr(media_access, "_correspondent_model_on", lambda: True)
    monkeypatch.setattr(media_access, "_LIMIT", SlidingWindowLimiter(10, 60))
    inbound = SimpleNamespace(mentions_bot=mentioned, identity=SimpleNamespace(
        user_id="sender", source=SimpleNamespace(surface_id="telegram")))
    assert media_access.paid_media_allowed(None, inbound) is allowed


def test_admitted_sender_has_a_paid_media_budget(monkeypatch):
    monkeypatch.setattr(media_access, "resolve_access_tier", lambda *a: AccessTier.OWNER)
    monkeypatch.setattr(media_access, "_LIMIT", SlidingWindowLimiter(10, 60))
    inbound = SimpleNamespace(mentions_bot=False, identity=SimpleNamespace(
        user_id="sender", source=SimpleNamespace(surface_id="telegram")))
    assert all(media_access.paid_media_allowed(None, inbound) for _ in range(10))
    assert not media_access.paid_media_allowed(None, inbound)



def _dm(surface="telegram", chat_type="dm", chat_role=None, mentioned=False):
    return SimpleNamespace(mentions_bot=mentioned, identity=SimpleNamespace(
        user_id="sender", chat_role=chat_role,
        source=SimpleNamespace(surface_id=surface, chat_type=chat_type, chat_id="-100")))


def test_allowlisted_telegram_dm_keeps_voice_with_the_model_off(monkeypatch):
    """CHAT-4 collateral: with the correspondent model off, Telegram's
    allowlist already admitted the DM; its voice follows its text."""
    monkeypatch.setattr(media_access, "resolve_access_tier", lambda *a: AccessTier.DENIED)
    monkeypatch.setattr(media_access, "_LIMIT", SlidingWindowLimiter(10, 60))
    monkeypatch.setattr(media_access, "_correspondent_model_on", lambda: False)
    assert media_access.paid_media_allowed(None, _dm()) is True
    assert media_access.paid_media_allowed(None, _dm(surface="whatsapp")) is False
    monkeypatch.setattr(media_access, "_correspondent_model_on", lambda: True)
    assert media_access.paid_media_allowed(None, _dm()) is False


@pytest.mark.parametrize("mode,role,allowed", [
    ("off", "owner", False), ("listen", "member", False),
    ("listen", "admin", True), ("mention", "member", True)])
def test_room_mode_gates_voice_like_text(monkeypatch, tmp_path, mode, role, allowed):
    """CHAT-4: a room in `off`/`listen` buys no transcription for a member."""
    from core.surfaces import chat_policy
    monkeypatch.setattr(media_access, "resolve_access_tier",
                        lambda *a: AccessTier.GROUP_MEMBER)
    monkeypatch.setattr(media_access, "_LIMIT", SlidingWindowLimiter(10, 60))
    base = chat_policy.load_for_chat(str(tmp_path), "telegram", "-100")
    from dataclasses import replace
    monkeypatch.setattr(chat_policy, "load_for_chat",
                        lambda *a, **k: replace(base, mode=mode))
    container = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)))
    got = media_access.paid_media_allowed(
        container, _dm(chat_type="supergroup", chat_role=role, mentioned=True))
    assert got is allowed


def test_owner_voice_marks_the_priority_slot(monkeypatch):
    monkeypatch.setattr(media_access, "_LIMIT", SlidingWindowLimiter(10, 60))
    monkeypatch.setattr(media_access, "resolve_access_tier", lambda *a: AccessTier.OWNER)
    assert media_access.paid_media_allowed(None, _dm()) is True
    assert media_access.OWNER_VOICE.get() is True
    monkeypatch.setattr(media_access, "resolve_access_tier",
                        lambda *a: AccessTier.CORRESPONDENT)
    assert media_access.paid_media_allowed(None, _dm()) is True
    assert media_access.OWNER_VOICE.get() is False


def test_declared_size_bounds():
    assert media_access.declared_voice_too_large(601, None)
    assert media_access.declared_voice_too_large(None, 21 * 1024 * 1024)
    assert not media_access.declared_voice_too_large(30, 100_000)
    assert not media_access.declared_voice_too_large(None, None)
