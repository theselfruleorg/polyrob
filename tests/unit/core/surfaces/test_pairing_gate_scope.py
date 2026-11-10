"""The pairing gate judges a PERSON's DM on a signed surface only. A room, a forgeable
sender, a bot, a synthetic turn and a bound correspondent never take a code and never
grow pairing.db; a flood of strangers is capped."""
import sqlite3
from types import SimpleNamespace

import pytest

from core.pairing import PairingStore
from core.surfaces.dispatcher import _pairing_applies
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


def _inbound(surface="slack", chat_type="dm", raw="U0X", **kw):
    src = SessionSource(surface_id=surface, chat_id="c1", chat_type=chat_type)
    return InboundMessage(text="hi", identity=Identity(user_id="u_x", source=src,
                                                       raw_user_id=raw), **kw)


class _Registry:
    def __init__(self, bound=()):
        self.bound = set(bound)

    def resolve(self, *, surface, address, thread_id=None):
        return object() if (surface, address) in self.bound else None


def _container(registry=None):
    return SimpleNamespace(get_service=lambda name: registry
                           if name == "correspondent_registry" else None)


def test_a_persons_dm_on_a_signed_surface_is_gated():
    assert _pairing_applies(_container(_Registry()), _inbound()) is True
    assert _pairing_applies(None, _inbound(surface="telegram")) is True


@pytest.mark.parametrize("inbound", [
    _inbound(chat_type="group"),                 # a room has its own allowlist + roles
    _inbound(surface="email", raw="a@b.example"),  # forgeable: correspondent-or-denied
    _inbound(sender_is_bot=True),
    _inbound(internal=True),
])
def test_rooms_forgeable_bots_and_synthetic_turns_are_not_gated(inbound):
    assert _pairing_applies(_container(_Registry()), inbound) is False


def test_a_bound_correspondent_is_not_gated():
    reg = _Registry({("slack", "U0X")})
    assert _pairing_applies(_container(reg), _inbound()) is False
    assert _pairing_applies(_container(reg), _inbound(raw="U0Y")) is True


def test_a_correspondent_probe_fault_keeps_the_gate():
    class _Boom:
        def resolve(self, **kw):
            raise RuntimeError("db locked")
    assert _pairing_applies(_container(_Boom()), _inbound()) is True


def test_a_flood_of_strangers_is_capped(tmp_path, monkeypatch):
    s = PairingStore(str(tmp_path / "p.db"))
    monkeypatch.setattr(PairingStore, "MAX_PENDING", 3)
    codes = [s.request(f"u_{i}") for i in range(5)]
    assert all(codes[:3]) and codes[3:] == [None, None]
    assert s.pending_count() == 3
    assert s.request("u_0") == codes[0]            # a waiting sender keeps its code


def test_two_processes_adding_the_new_columns_do_not_fail(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paired_users (user_id TEXT PRIMARY KEY, code TEXT, "
                 "paired INTEGER NOT NULL DEFAULT 0, created_at REAL)")
    conn.commit()
    conn.close()
    PairingStore(path)
    PairingStore(path)                              # the columns exist: no error


def test_by_code_names_who_is_approved(tmp_path):
    s = PairingStore(str(tmp_path / "p.db"))
    code = s.request("u_a", surface="discord", raw_id="1001")
    assert s.by_code(code) == {"user_id": "u_a", "surface": "discord", "raw_id": "1001"}
    s.approve(code)
    assert s.by_code(code) is None


def test_a_paired_sender_has_the_owner_tier_but_none_of_the_owners_powers(tmp_path, monkeypatch):
    """Pairing gives owner ROUTING under the sender's own tenant: every owner-tenant
    gate (money, approvals, owner verbs, host code) still refuses them."""
    from core.instance import is_owner_uid
    from core.money.authority import owner_refusal
    from core.surfaces.access import AccessTier, resolve_access_tier
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    s = PairingStore(str(tmp_path / "pairing.db"))
    s.approve(s.request("u_friend", surface="slack", raw_id="U0F"))
    c = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)),
                        get_service=lambda name: None)
    ident = Identity(user_id="u_friend", raw_user_id="U0F",
                     source=SessionSource(surface_id="slack", chat_id="D1", chat_type="dm"))
    assert resolve_access_tier(c, ident, env={"POLYROB_OWNER_USER_ID": "u_owner", "POLYROB_REQUIRE_PAIRING": "1"}) \
        == AccessTier.OWNER
    assert is_owner_uid("u_friend") is False
    assert owner_refusal("u_friend") is not None
    from surfaces.telegram.harness import _is_admin_owner
    assert _is_admin_owner("u_friend") is False


def test_the_pair_verb_lists_approves_and_revokes(tmp_path, monkeypatch):
    from core.pairing import pair_verb
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    s = PairingStore(str(tmp_path / "p.db"))
    assert "Nobody is paired" in pair_verb([], store=s)
    code = s.request("u_a", surface="slack", raw_id="U0A")
    listed = pair_verb(["list"], store=s)
    assert "waiting  slack:U0A" in listed and code in listed
    assert "No waiting pairing request from discord" in pair_verb(
        ["approve", "discord", code], store=s)              # the code is surface-scoped
    done = pair_verb(["approve", "slack", code], store=s)
    assert done.startswith("✅ Paired slack:U0A") and "never your wallet" in done
    assert s.is_paired("u_a")
    assert pair_verb(["revoke", "slack", "U0A"], store=s) == "✅ Revoked slack U0A."
    assert not s.is_paired("u_a")
    assert pair_verb(["bogus"], store=s).startswith("Usage: /pair")


def test_doctor_line_says_off_on_and_counts(tmp_path):
    from core.pairing import status_line, store_for
    assert status_line({}).startswith("pairing: off")
    env = {"POLYROB_REQUIRE_PAIRING": "true", "POLYROB_DATA_DIR": str(tmp_path)}
    assert "nobody paired or waiting yet" in status_line(env)
    assert not (tmp_path / "pairing.db").exists()           # doctor creates no store
    st = store_for(str(tmp_path))
    st.approve(st.request("u_a", surface="slack", raw_id="U0A"))
    st.request("u_b", surface="slack", raw_id="U0B")
    assert status_line(env) == "pairing: on — 1 paired, 1 waiting (/pair)"


def test_pair_is_an_owner_verb_on_every_owner_seat():
    from core.surfaces.dispatcher import _COMMANDS
    from core.verbs import room_refused, verb_for
    from surfaces.telegram.admin_verbs import ADMIN_VERBS
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    assert verb_for("/pair") is not None and room_refused("/pair")
    assert "/pair" in _COMMANDS and "/pair" in _OWNER_ADMIN_COMMANDS and "/pair" in ADMIN_VERBS


def test_the_code_reply_tells_the_owner_verb():
    from core.pairing import code_reply
    assert code_reply("slack", "abc").endswith("Ask my owner to send me: /pair approve slack abc")


def test_expired_requests_never_hold_the_cap(tmp_path, monkeypatch):
    import time as _t
    s = PairingStore(str(tmp_path / "p.db"))
    monkeypatch.setattr(PairingStore, "MAX_PENDING", 2)
    s.request("u_0")
    s.request("u_1")
    real = _t.time
    monkeypatch.setattr("core.pairing.time.time", lambda: real() + 16 * 60)
    assert s.request("u_new")                       # the two dead codes made room
    assert s.pending_count() == 1


def test_a_new_code_after_expiry_is_told_at_once(tmp_path, monkeypatch):
    import time as _t
    s = PairingStore(str(tmp_path / "p.db"))
    first = s.request("u_a")
    assert s.should_tell("u_a")
    real = _t.time
    monkeypatch.setattr("core.pairing.time.time", lambda: real() + 16 * 60)
    second = s.request("u_a")
    assert second != first and s.should_tell("u_a", now=real() + 16 * 60)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _tg_update(tg_id=4242, text="hello"):
    return {"update_id": 9, "message": {"chat": {"id": tg_id, "type": "private"},
                                        "from": {"id": tg_id}, "text": text}}


@pytest.mark.asyncio
async def test_telegram_stranger_gets_a_code_and_a_paired_one_passes(tmp_path, monkeypatch):
    from surfaces.telegram import storefront_ops
    from tools.user_directory import UserDirectory
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    async def _no_store(h, u):
        return False
    monkeypatch.setattr(storefront_ops, "_storefront", _no_store)
    h = SimpleNamespace(bot=_Bot(), container=SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path)), get_service=lambda n: None))
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "answered"
    assert "/pair approve telegram " in h.bot.sent[0][1]
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "raw_allowlist"
    from core.pairing import store_for
    st = store_for(str(tmp_path))
    assert st.approve(h.bot.sent[0][1].rsplit(" ", 1)[1], surface="telegram")
    assert st.is_paired(UserDirectory._derive_internal_id("telegram", "4242"))
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "paired"


@pytest.mark.asyncio
async def test_telegram_stranger_is_ignored_when_pairing_is_off(tmp_path, monkeypatch):
    from surfaces.telegram import storefront_ops
    monkeypatch.delenv("POLYROB_REQUIRE_PAIRING", raising=False)

    async def _no_store(h, u):
        return False
    monkeypatch.setattr(storefront_ops, "_storefront", _no_store)
    h = SimpleNamespace(bot=_Bot(), container=None)
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "raw_allowlist"
    assert h.bot.sent == []


@pytest.mark.asyncio
async def test_a_telegram_correspondent_passes_the_dm_lock_only_with_the_tier_model(monkeypatch):
    """113 W0 (F-02): the reply of a person the agent wrote to was dropped at the raw
    DM lock. It passes as "correspondent" only while the tier model is on (the
    dispatcher then routes it as DATA); a fault or an unknown sender stays a stranger."""
    from core.surfaces.config import SurfaceConfig
    from surfaces.telegram import storefront_ops
    monkeypatch.delenv("POLYROB_REQUIRE_PAIRING", raising=False)

    async def _no_store(h, u):
        return False
    monkeypatch.setattr(storefront_ops, "_storefront", _no_store)
    h = SimpleNamespace(bot=_Bot(), container=_container(_Registry({("telegram", "4242")})))
    monkeypatch.setattr(SurfaceConfig, "correspondent_access_enabled", staticmethod(lambda: True))
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "correspondent"
    assert await storefront_ops._stranger_door(h, _tg_update(tg_id=7), 7) == "raw_allowlist"

    class _Boom:
        def resolve(self, **kw):
            raise RuntimeError("db")
    hb = SimpleNamespace(bot=_Bot(), container=_container(_Boom()))
    assert await storefront_ops._stranger_door(hb, _tg_update(), 4242) == "raw_allowlist"

    monkeypatch.setattr(SurfaceConfig, "correspondent_access_enabled", staticmethod(lambda: False))
    assert await storefront_ops._stranger_door(h, _tg_update(), 4242) == "raw_allowlist"
    assert h.bot.sent == []


def test_the_pair_verb_never_approves_a_bare_code(tmp_path, monkeypatch):
    """`/pair approve <code>` skipped the surface check, so a stranger on one chat app
    who passed his code off as another app's got paired."""
    from core.pairing import pair_verb
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    s = PairingStore(str(tmp_path / "p.db"))
    code = s.request("u_a", surface="discord", raw_id="D0A")
    reply = pair_verb(["approve", code], store=s)
    assert reply.startswith("Name the chat app") and code in reply
    assert not s.is_paired("u_a")
