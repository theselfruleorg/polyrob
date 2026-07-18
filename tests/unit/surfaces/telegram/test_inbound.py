"""P4: the Telegram inbound pipeline (dedup -> identify -> route), no aiogram/network.

Order is load-bearing (Fusion): dedup runs FIRST (before identify, which writes via
get_or_create_by_tg_id, and before route, which may create a session) so a redelivered
update_id never double-processes. Composes P4 dedup + P2c UserDirectory + P3 route_inbound.
"""
import pytest

from surfaces.telegram.inbound import process_update, build_inbound_message
from surfaces.telegram.dedup import UpdateDedup
from core.surfaces.dispatcher import RouteKind
from core.surfaces.session_chat_registry import SessionChatRegistry, build_session_key
from tools.user_directory import UserDirectory


class _Container:
    def __init__(self, registry): self._svc = {"session_chat_registry": registry}
    def get_service(self, n): return self._svc.get(n)


def _update(update_id, text, chat_id=555, chat_type="private", from_id=555):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": from_id, "username": "alice"},
        },
    }


def _deps(tmp_path):
    return (
        UpdateDedup(str(tmp_path / "dedup.db")),
        UserDirectory(str(tmp_path / "users.db")),
        SessionChatRegistry(str(tmp_path / "chat.db")),
    )


def test_build_inbound_identifies_internal_user(tmp_path):
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(_update(1, "hi", from_id=12345), ud)
    assert inbound.identity.raw_user_id == "12345"
    assert inbound.identity.user_id.startswith("u_")  # internal, not the tg id
    assert inbound.identity.user_id != "12345"
    assert inbound.identity.source.surface_id == "telegram"
    assert inbound.identity.source.chat_type == "dm"   # private -> dm
    assert inbound.idempotency_key == "1"


@pytest.mark.asyncio
async def test_new_update_routes_to_task_agent(tmp_path):
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)
    result = await process_update(c, _update(1, "do a thing"), dedup=dedup, user_directory=ud)
    assert result is not None
    assert result.decision.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_redelivered_update_is_dropped(tmp_path):
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)
    first = await process_update(c, _update(7, "hi"), dedup=dedup, user_directory=ud, now=1000.0)
    second = await process_update(c, _update(7, "hi"), dedup=dedup, user_directory=ud, now=1000.5)
    assert first is not None
    assert second is None  # dropped by dedup, no double-processing


@pytest.mark.asyncio
async def test_command_routes_to_command(tmp_path):
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)
    result = await process_update(c, _update(2, "/cancel"), dedup=dedup, user_directory=ud)
    assert result.decision.kind == RouteKind.COMMAND
    assert result.decision.command == "/cancel"


@pytest.mark.asyncio
async def test_warm_session_steers(tmp_path):
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)
    # Pre-bind the session for this tg user's DM key.
    inbound = build_inbound_message(_update(3, "continue"), ud)
    key = build_session_key(inbound.identity.source, inbound.identity.user_id)
    reg.bind(key, "sess_1", inbound.identity.user_id, "telegram", "555")
    result = await process_update(c, _update(4, "continue"), dedup=dedup, user_directory=ud)
    assert result.decision.kind == RouteKind.STEER
    assert result.decision.session_id == "sess_1"


@pytest.mark.asyncio
async def test_non_message_update_is_ignored(tmp_path):
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)
    result = await process_update(c, {"update_id": 9}, dedup=dedup, user_directory=ud)
    assert result is None  # nothing to route


@pytest.mark.asyncio
async def test_voice_transcript_is_marked_as_voice(tmp_path):
    # A transcribed voice note must reach the agent MARKED as voice, so the agent
    # knows it CAN process voice (else it denies having speech-to-text — the 2026-07-03
    # "Can you understand my voice messages?" -> "no" self-awareness bug).
    dedup, ud, reg = _deps(tmp_path)
    c = _Container(reg)

    async def _fake_transcribe(update):
        return "what did you run last hour"

    voice_update = {
        "update_id": 42,
        "message": {
            "voice": {"file_id": "vf_1"},
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555, "username": "alice"},
        },
    }
    result = await process_update(
        c, voice_update, dedup=dedup, user_directory=ud, transcribe_voice=_fake_transcribe
    )
    assert result is not None
    assert "what did you run last hour" in result.inbound.text  # transcript preserved
    assert result.inbound.text.lower().startswith("[voice")      # marked as voice
    # The clean transcript is still stamped on the media for voice-echo (no marker there).
    assert result.inbound.media
    assert result.inbound.media[0].transcript == "what did you run last hour"


# --- owner ⇄ instance user_id alias (2026-07-03) -----------------------------
# The authenticated Telegram owner operates as the instance OWNER principal (e.g.
# "rob") so the owner's chat shares autonomy's tenant (goals/memory/SELF). A
# non-owner tg id keeps its surface-hashed u_ id (tenant isolation intact).

_OWNER_TG = "28436760"


def _owner_env(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", _OWNER_TG)


def test_owner_tg_id_aliases_to_owner_principal(tmp_path, monkeypatch):
    _owner_env(monkeypatch)
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(_update(1, "list my goals", from_id=_OWNER_TG), ud)
    assert inbound.identity.user_id == "rob"       # aliased to the owner principal
    assert inbound.identity.raw_user_id == _OWNER_TG  # raw sender id preserved


def test_non_owner_tg_id_still_hashes(tmp_path, monkeypatch):
    _owner_env(monkeypatch)
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(_update(2, "hi", from_id="99887766"), ud)
    assert inbound.identity.user_id.startswith("u_")  # unchanged: surface-hashed id
    assert inbound.identity.user_id != "rob"


def test_no_owner_env_all_senders_hash(tmp_path, monkeypatch):
    # No owner principal bound -> byte-identical legacy behaviour for everyone.
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.delenv("SURFACE_SUPER_ADMIN_USER_IDS", raising=False)
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(_update(3, "hi", from_id=_OWNER_TG), ud)
    assert inbound.identity.user_id.startswith("u_")


# --- W3 groups (2026-07-14 battle-test fix): telegram must actually DETECT mentions ---
# The dispatcher's group gate reads inbound.mentions_bot; telegram never set it, so
# EVERY group message (owner @mentions and replies included) was silently denied.

def _group_update(update_id, text, *, entities=None, reply_to_from=None, from_id=777):
    msg = {
        "text": text,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": from_id, "username": "alice"},
    }
    if entities is not None:
        msg["entities"] = entities
    if reply_to_from is not None:
        msg["reply_to_message"] = {"message_id": 42, "from": reply_to_from}
    return {"update_id": update_id, "message": msg}


def test_group_entity_mention_of_bot_sets_mentions_bot(tmp_path):
    _, ud, _ = _deps(tmp_path)
    text = "hey @tmachinroBot what can you do?"
    ents = [{"type": "mention", "offset": 4, "length": 13}]
    inbound = build_inbound_message(_group_update(10, text, entities=ents), ud,
                                    bot_username="tmachinroBot")
    assert inbound.mentions_bot is True


def test_group_mention_of_other_user_is_not_a_bot_mention(tmp_path):
    _, ud, _ = _deps(tmp_path)
    text = "hey @someoneelse what's up"
    ents = [{"type": "mention", "offset": 4, "length": 12}]
    inbound = build_inbound_message(_group_update(11, text, entities=ents), ud,
                                    bot_username="tmachinroBot")
    assert inbound.mentions_bot is False


def test_group_reply_to_bot_message_counts_as_mention(tmp_path):
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(
        _group_update(12, "yes please", reply_to_from={"id": 999, "is_bot": True,
                                                       "username": "tmachinroBot"}),
        ud, bot_username="tmachinroBot")
    assert inbound.mentions_bot is True
    assert inbound.reply_to == "42"


def test_group_reply_to_other_bot_is_not_ours(tmp_path):
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(
        _group_update(13, "ok", reply_to_from={"id": 998, "is_bot": True,
                                               "username": "otherbot"}),
        ud, bot_username="tmachinroBot")
    assert inbound.mentions_bot is False


def test_group_plain_message_without_username_stays_unknown(tmp_path):
    _, ud, _ = _deps(tmp_path)
    # bot_username unknown -> None (gate treats as not-mentioned; legacy-safe)
    inbound = build_inbound_message(_group_update(14, "just chatting"), ud)
    assert inbound.mentions_bot is None


def test_group_text_mention_case_insensitive(tmp_path):
    _, ud, _ = _deps(tmp_path)
    text = "@TMACHINROBOT hello"
    ents = [{"type": "mention", "offset": 0, "length": 13}]
    inbound = build_inbound_message(_group_update(15, text, entities=ents), ud,
                                    bot_username="tmachinroBot")
    assert inbound.mentions_bot is True


def test_private_chat_mentions_stays_none(tmp_path):
    _, ud, _ = _deps(tmp_path)
    inbound = build_inbound_message(_update(16, "hi there"), ud, bot_username="tmachinroBot")
    assert inbound.mentions_bot is None
