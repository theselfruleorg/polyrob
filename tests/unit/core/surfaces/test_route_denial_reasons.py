"""045 I2 — a denial the owner can READ, not merely count.

``RouteDecision`` carried no ``reason``, so ``record_route``'s
``getattr(decision, "reason", None)`` was always ``None`` and the rollup's
``v IS NOT NULL`` filter dropped every row: ``top_denial_reasons`` was
STRUCTURALLY always empty, and seven distinct deny branches collapsed into one
indistinguishable "denied".

The slugs are a VOCABULARY the owner reads — short, lowercase and stable. One
test per branch, so a renamed or dropped slug fails loudly rather than
degrading the brief back to "38 denied, cause unknown".
"""
import json
import types

import pytest

from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.session_chat_registry import SessionChatRegistry


class _Container:
    def __init__(self, tmp_path, **svc):
        self._svc = {k: v for k, v in svc.items() if v is not None}
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, name):
        return self._svc.get(name)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("POLYROB_REQUIRE_PAIRING", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SURFACE_SUPER_ADMIN_USER_IDS", "CORRESPONDENT_ACCESS_ENABLED",
              "GROUP_CHAT_ENABLED", "GROUP_REQUIRE_MENTION", "SESSION_RESET_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_RESET_MODE", "none")


@pytest.fixture(autouse=True)
def _reset_owner_tenant_cache():
    from core.surfaces import access_log
    access_log._OWNER_TENANT = None
    yield
    access_log._OWNER_TENANT = None


@pytest.fixture
def tele_db(tmp_path, monkeypatch):
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    yield str(p)
    el._INSTANCES.clear()


def _recorded(db_path) -> dict:
    """The attrs of the ONE row this test wrote."""
    from core.sqlite_util import execute_retry
    rows = [dict(r) for r in (execute_retry(
        db_path, "SELECT kind, attrs FROM telemetry_events", (),
        fetch="all") or [])]
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    assert rows[0]["kind"] == "access_denied"
    return json.loads(rows[0]["attrs"])


def _group(text="hi", *, user="u_stranger", surface="discord", chat="chan-1",
           mentions_bot=None, sender_is_bot=False):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type="group")
    return InboundMessage(text=text, mentions_bot=mentions_bot,
                          sender_is_bot=sender_is_bot,
                          identity=Identity(user_id=user, source=src,
                                            raw_user_id=user))


def _dm(text="hi", *, user="u_stranger", surface="telegram", chat="c1"):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type="dm")
    return InboundMessage(text=text,
                          identity=Identity(user_id=user, source=src,
                                            raw_user_id=user))


@pytest.mark.asyncio
async def test_pairing_required(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path)
    d = await route_inbound(c, _dm(user="u_stranger"))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "pairing_required"


@pytest.mark.asyncio
async def test_group_disabled(tele_db, tmp_path):
    d = await route_inbound(_Container(tmp_path), _group())
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "group_disabled"


@pytest.mark.asyncio
async def test_tier_denied_in_a_group(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    # chat is NOT allowlisted -> resolve_access_tier says DENIED. The surface bus
    # IS installed here (044 C2 denies earlier, with its own slug, when it is not).
    c = _Container(tmp_path,
                   session_chat_registry=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group(mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    a = _recorded(tele_db)
    assert a["reason"] == "tier_denied"
    assert a["tier"] == "denied"


@pytest.mark.asyncio
async def test_rooms_need_singular_chat(tele_db, tmp_path, monkeypatch):
    """044 C2: GROUP_CHAT_ENABLED on, the Singular Chat bus absent. The room rail
    is BUILT on that bus (chat<->session registry, caps, ledger, outbound
    router); running half-built meant a fresh unbound session per mention,
    answered through the raw bot with no [SILENT] rule, no secret scrub and no
    reply cap. Refuse, silently, naming the flag in the log."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    d = await route_inbound(_Container(tmp_path), _group(mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    assert d.silent is True
    assert _recorded(tele_db)["reason"] == "rooms_need_singular_chat"


@pytest.mark.asyncio
async def test_rooms_need_singular_chat_leaves_dms_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    d = await route_inbound(_Container(tmp_path), _dm(user="u_stranger"))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_bot_loop_guard(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    caps = types.SimpleNamespace(may_trigger=lambda *a, **k: (True, ""),
                                 record_trigger=lambda *a, **k: None)
    c = _Container(tmp_path, room_caps=caps,
                   session_chat_registry=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group(mentions_bot=False, sender_is_bot=True))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "bot_loop_guard"


@pytest.mark.asyncio
async def test_room_cap(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    caps = types.SimpleNamespace(may_trigger=lambda *a, **k: (False, "per_chat_cap"),
                                 record_trigger=lambda *a, **k: None)
    c = _Container(tmp_path, room_caps=caps,
                   session_chat_registry=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group(mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "room_cap"


@pytest.mark.asyncio
async def test_no_mention(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path,
                   session_chat_registry=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group(user="u_owner", mentions_bot=False))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "no_mention"


@pytest.mark.asyncio
async def test_member_with_no_session_is_no_longer_a_denial(tele_db, tmp_path, monkeypatch):
    """044 T14 retired the ``participant_no_session`` slug: a member MAY start
    the room session, so this is an allowed route that still records its tier."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path,
                   session_chat_registry=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group(mentions_bot=True))
    assert d.kind == RouteKind.TASK_AGENT
    from core.sqlite_util import execute_retry
    rows = [dict(r) for r in (execute_retry(
        tele_db, "SELECT attrs FROM telemetry_events", (), fetch="all") or [])]
    assert json.loads(rows[0]["attrs"])["tier"] == "group_member"


def test_the_denial_vocabulary_has_no_dead_slug():
    """Every slug names a LIVE branch. ``participant_no_session`` outlived its
    branch when 044 T14 let a member start the room session — a slug the owner
    can never see again is a lie in the vocabulary."""
    import inspect

    import core.surfaces.dispatcher as disp
    src = inspect.getsource(disp._route_inbound_impl)
    dead = [r for r in disp.DENIAL_REASONS if f'reason="{r}"' not in src]
    assert not dead, f"DENIAL_REASONS slug(s) with no branch: {dead}"


@pytest.mark.asyncio
async def test_group_fault_fails_closed_with_its_own_slug(tele_db, tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")

    class _Boom:
        def resolve(self, *a, **k):
            raise RuntimeError("registry down")

    c = _Container(tmp_path, session_chat_registry=_Boom())
    import core.surfaces.access as access
    monkeypatch.setattr(access, "resolve_access_tier",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = await route_inbound(c, _group(mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "group_fault"


@pytest.mark.asyncio
async def test_tier_denied_on_a_dm(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path)
    d = await route_inbound(c, _dm(user="u_stranger"))
    assert d.kind == RouteKind.DENIED
    a = _recorded(tele_db)
    assert a["reason"] == "tier_denied"
    assert a["tier"] == "denied"


@pytest.mark.asyncio
async def test_correspondent_with_no_origin_session(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    import core.surfaces.access as access
    from core.surfaces.access import AccessTier
    monkeypatch.setattr(access, "resolve_access_tier",
                        lambda *a, **k: AccessTier.CORRESPONDENT)
    corr = types.SimpleNamespace(resolve=lambda **k: None)
    c = _Container(tmp_path, correspondent_registry=corr)
    d = await route_inbound(c, _dm(user="u_stranger"))
    assert d.kind == RouteKind.DENIED
    a = _recorded(tele_db)
    assert a["reason"] == "no_origin_session"
    assert a["tier"] == "correspondent"


@pytest.mark.asyncio
async def test_tier_fault_fails_closed_with_its_own_slug(tele_db, tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    import core.surfaces.access as access
    monkeypatch.setattr(access, "resolve_access_tier",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = await route_inbound(_Container(tmp_path), _dm())
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "tier_fault"


@pytest.mark.asyncio
async def test_forgeable_sender(tele_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    d = await route_inbound(_Container(tmp_path),
                            _dm(user="a@b.com", surface="email", chat="a@b.com"))
    assert d.kind == RouteKind.DENIED
    assert _recorded(tele_db)["reason"] == "forgeable_sender"


@pytest.mark.asyncio
async def test_an_allowed_route_carries_its_tier(tele_db, tmp_path, monkeypatch):
    """`tier` had the same defect as `reason`: the parameter existed, the
    allowlist carried it, every unit test passed it, and the one PRODUCTION
    call site never did."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    d = await route_inbound(_Container(tmp_path), _dm(user="u_owner"))
    assert d.kind == RouteKind.TASK_AGENT
    from core.sqlite_util import execute_retry
    rows = [dict(r) for r in (execute_retry(
        tele_db, "SELECT attrs FROM telemetry_events", (), fetch="all") or [])]
    assert json.loads(rows[0]["attrs"])["tier"] == "owner"


def test_every_denied_return_site_sets_a_reason():
    """A ratchet on the branch that made this class possible: a future DENIED
    return with no reason puts the owner back to 'denied, cause unknown'."""
    import inspect
    import re

    import core.surfaces.dispatcher as disp
    src = inspect.getsource(disp._route_inbound_impl)
    # Normalise line continuations so a wrapped return reads as one statement.
    flat = re.sub(r"\s+", " ", src)
    sites = re.findall(r"return RouteDecision\(RouteKind\.DENIED,.*?\)", flat)
    assert sites, "no DENIED return sites found — the scan is broken, not the code"
    missing = [s for s in sites if "reason=" not in s]
    assert not missing, f"DENIED return(s) with no reason slug: {missing}"
