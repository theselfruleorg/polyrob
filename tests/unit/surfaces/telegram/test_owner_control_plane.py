"""Chat is the control plane (chat-first review 2026-08-22, G9/G10/G12).

Under the current posture the owner can authorise autonomous spend from a phone.
Before this, they could not STOP it from a phone: `owner halt` was CLI-only, and
`/pending` silently omitted the pending correspondents the CLI showed. These
tests pin the three verbs that close that gap, plus the command menu that makes
them discoverable.
"""
import os

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS, act_on_inbound, help_commands
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(command, text, user_id="gleb"):
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user_id, source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key="telegram:1",
                             session_id=None, command=command)
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# G9 — the kill-switch reaches the phone
# ---------------------------------------------------------------------------

def test_halt_verbs_are_owner_admin_commands():
    assert "/halt" in _OWNER_ADMIN_COMMANDS
    assert "/resume" in _OWNER_ADMIN_COMMANDS


@pytest.mark.asyncio
async def test_halt_then_resume_round_trip(env):
    from core.config_policy import AutonomyConfig

    out = await act_on_inbound(_Agent(str(env)), _cmd("/halt", "/halt"))
    assert "HALTED" in out or "Paused everything" in out
    from core.autonomy_control import PAUSE_FILENAME
    assert os.path.exists(os.path.join(str(env), PAUSE_FILENAME))  # 031: the one record
    assert AutonomyConfig.autonomy_halted() is True

    out = await act_on_inbound(_Agent(str(env)), _cmd("/resume", "/resume"))
    assert "RESUMED" in out
    assert not os.path.exists(os.path.join(str(env), PAUSE_FILENAME))
    assert AutonomyConfig.autonomy_halted() is False


@pytest.mark.asyncio
async def test_resume_is_honest_about_an_env_halt(env, monkeypatch):
    """A halt set in the environment survives `resume`. Reporting success there
    would be a lie the owner acts on."""
    await act_on_inbound(_Agent(str(env)), _cmd("/halt", "/halt"))
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/resume", "/resume"))
    assert "RESUMED" not in out
    assert "AUTONOMY_HALT" in out


@pytest.mark.asyncio
async def test_halt_refused_for_non_owner(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/halt", "/halt", user_id="stranger"))
    assert "Owner only" in out
    assert not os.path.exists(os.path.join(str(env), "AUTONOMY_HALT"))


# ---------------------------------------------------------------------------
# G10 — /pending shows what the CLI shows
# ---------------------------------------------------------------------------

def _seed_pending_correspondent(data_dir, tenant="gleb"):
    from core.surfaces.correspondents import CorrespondentRegistry
    reg = CorrespondentRegistry(os.path.join(str(data_dir), "correspondents.db"))
    reg.seed(surface="email", address="third@party.example", session_id="s-1",
             user_id=tenant, require_approval=True)
    return reg


@pytest.mark.asyncio
async def test_pending_lists_pending_correspondents(env):
    _seed_pending_correspondent(env)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    assert "third@party.example" in out
    assert "correspondent" in out


@pytest.mark.asyncio
async def test_approve_activates_a_pending_correspondent(env):
    reg = _seed_pending_correspondent(env)
    out = await act_on_inbound(
        _Agent(str(env)), _cmd("/approve", "/approve email:third@party.example"))
    assert "Approved" in out
    states = {r["state"] for r in reg.list(user_id="gleb")}
    assert states == {"active"}


@pytest.mark.asyncio
async def test_reject_tombstones_the_pending_binding(env):
    """030 C3: the registry gained a real reject() — chat now tombstones the
    pending binding (blocks a silent re-seed), matching the webview Review page."""
    _seed_pending_correspondent(env)
    out = await act_on_inbound(
        _Agent(str(env)), _cmd("/reject", "/reject email:third@party.example"))
    assert "Rejected" in out and "tombstoned" in out


# ---------------------------------------------------------------------------
# G12 — the mobile command menu, sourced from the help SSOT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_help_advertises_prose_driven_admin(env):
    """G11: the agent can act through its own tools when asked in plain words,
    and that path is strictly wider than the slash list. /help read as an
    exhaustive list of what was possible, so nobody used it."""
    out = await act_on_inbound(_Agent(str(env)), _cmd("/help", "/help"))
    assert "don't have to use a command" in out
    assert "schedule a check every morning" in out


def test_help_commands_parse_from_the_help_body():
    pairs = dict(help_commands())
    assert "halt" in pairs and "resume" in pairs
    assert "help" in pairs and "task" in pairs
    for name, desc in pairs.items():
        assert name == name.lower() and name.replace("_", "").isalnum()
        assert desc and len(desc) <= 256
        assert not desc.startswith("—")


def test_help_commands_has_no_duplicate_names():
    """`setMyCommands` rejects a duplicate command name in ONE call — and
    `_publish_command_menu` wraps that call in a broad `except Exception:
    logger.debug(...)`, so a duplicate would silently stop the ENTIRE menu
    from refreshing, not just the offending entry. `dict(help_commands())`
    and `{name for name, _ in ...}` (the two existing tests above) both
    silently collapse a duplicate key, so neither can catch this — this test
    asserts on the raw list instead. A sub-line under an existing verb (e.g.
    `/goal objective ...` under `/goal ...`) is a real case, not a hypothetical:
    it shipped once and produced exactly this duplicate.
    """
    names = [name for name, _ in help_commands()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate command name(s) in help_commands(): {dupes}"


def test_command_menu_covers_every_owner_admin_verb():
    """The menu is the discovery surface: an admin verb missing from it is a
    verb the owner has to already know about."""
    menu = {name for name, _ in help_commands()}
    missing = {c.lstrip("/") for c in _OWNER_ADMIN_COMMANDS} - menu
    # /journey is a deliberate alias of /recap and is not listed twice.
    assert missing == {"journey"}, f"verbs absent from the /help SSOT: {missing}"


def test_every_owner_verb_is_routable():
    """A handler for a verb the DISPATCHER doesn't know is dead code.

    `/halt` shipped this way: implemented, helped, menu-listed — and absent from
    `_COMMANDS`, so a real message fell through to STEER and reached the agent as
    chat text. Only the tests passed, because they built the RouteDecision by
    hand. This asserts the two lists agree.
    """
    from core.surfaces.dispatcher import _COMMANDS
    missing = set(_OWNER_ADMIN_COMMANDS) - set(_COMMANDS)
    assert missing == set(), (
        f"verbs handled but not routable (add to core/surfaces/dispatcher.py "
        f"_COMMANDS): {sorted(missing)}")


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["/halt", "/resume", "/cron", "/goal",
                                  "/wallet", "/invoices", "/settle"])
async def test_owner_verbs_route_as_commands_end_to_end(env, verb):
    """The regression above, through the real router rather than a hand-built
    decision."""
    from core.surfaces.dispatcher import RouteKind, route_inbound
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=verb,
                             identity=Identity(user_id="gleb", source=source))
    decision = await route_inbound(None, inbound)
    assert decision.kind is RouteKind.COMMAND
    assert decision.command == verb


def test_every_routable_verb_is_discoverable():
    """030 WS-C6: the OTHER direction of the three-lists contract. The original
    guard only pinned handled ⊆ routable; a verb routable but absent from the
    _HELP_BODY SSOT would work yet be invisible in /help AND the setMyCommands
    menu. Exactly two deliberate exceptions exist."""
    from core.surfaces.dispatcher import _COMMANDS
    menu = {f"/{name}" for name, _ in help_commands()}
    hidden = set(_COMMANDS) - menu
    assert hidden == {"/journey", "/start"}, (
        f"routable verbs missing from the /help SSOT (add a _HELP_BODY line, "
        f"or pin a deliberate exception here): {sorted(hidden)}")
