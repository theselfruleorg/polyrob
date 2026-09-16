"""The owner's approval loop CLOSES from a phone (2026-09-15).

Six defects, all of them shaped the same way: the machinery to decide a pending
item existed, and the message the owner actually reads did not reach it.

* the proactive notice named a shell command and a plain-word reply, and
  nothing on the phone could run either;
* a self-evolution proposal was addressed by prose, so it could not be tapped;
* the bare ``/approve`` shortcut and ``/approve all`` read ONE of the three
  queues the listing right above them showed.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import act_on_inbound, normalize_tappable_command
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


def _cmd(command, text, user="alice"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(
        text=text, identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.instance import DEFAULT_INSTANCE_ID
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def _seed_pending_self(home, uid="alice"):
    from core.self_context_writer import PROVENANCE_AGENT, SelfContextWriter
    SelfContextWriter(home).propose(
        "Learned: escalate blockers to the owner proactively.",
        user_id=uid, created_by=PROVENANCE_AGENT, pending=True)


def _seed_tool_approval(home, uid="alice"):
    """An OPEN tool-approval ask — the second queue, the one that held money."""
    import os

    from agents.task.goals.board import GoalBoard
    from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND
    board = GoalBoard(os.path.join(str(home), "goals.db"))
    board.create_ask(user_id=uid, what="Approve defi_trade?", why="swap 5 USDC",
                     extra_payload={"ask_kind": TOOL_APPROVAL_ASK_KIND,
                                    "tool_name": "defi_trade",
                                    "params_summary": "swap 5 USDC",
                                    "request_hash": "h1"},
                     force=True)
    return board


# --- the tappable token -------------------------------------------------------

def test_a_prose_id_gets_a_tappable_token_and_it_round_trips():
    """A skill id carries its own separators, so it cannot BE the token. The
    short alias can, and what is rendered is what the router parses back."""
    from core.self_evolution import pending_tap_token, resolve_pending_alias

    item = {"kind": "skill", "id": "treasury-track-record-post"}
    token = pending_tap_token("approve", item)

    assert token.startswith("/approve_p_")
    # One token, auto-linkable: no space, and short enough for a client to link.
    assert " " not in token and len(token) <= 32

    verb, target = normalize_tappable_command(token)
    assert verb == "/approve"
    assert resolve_pending_alias(target, [item]) is item


def test_the_alias_is_stable_across_messages():
    """The alias printed in one message must still resolve in the next."""
    from core.self_evolution import pending_tap_alias

    assert (pending_tap_alias("skill", "dm-conversation")
            == pending_tap_alias("skill", "dm-conversation"))
    assert (pending_tap_alias("skill", "dm-conversation")
            != pending_tap_alias("skill", "meme-candidate-sourcing"))


def test_an_ambiguous_underscore_form_is_still_refused():
    """The old safety rule holds: only separator-free lanes are parsed."""
    assert normalize_tappable_command("/approve_dm_conversation") == (None, None)


def test_approve_all_is_one_token():
    assert normalize_tappable_command("/approve_all") == ("/approve", "all")


def test_the_notice_carries_a_tap_and_no_shell_command():
    """The live message (screenshot, 2026-09-15) ended in `polyrob owner
    pending` and `Reply "approve"`. The owner has no shell on a phone."""
    from core.self_evolution import build_pending_notification

    text = build_pending_notification([
        {"kind": "skill", "id": "dm-conversation", "preview": "How Rob handles DMs"},
        {"kind": "skill", "id": "meme-candidate-sourcing", "preview": "Hunting candidates"},
    ])

    assert "polyrob" not in text
    assert text.count("/approve_p_") == 2
    assert "/approve_all" in text


# --- the union ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_bare_approve_does_not_deny_a_queue_it_cannot_see(env):
    """With ONLY a tool approval waiting, the shortcut used to answer "Nothing
    pending — there is nothing waiting on you". That is a confident lie over
    real, money-shaped work."""
    _seed_tool_approval(env)

    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve"))

    assert "nothing waiting on you" not in out.lower()
    assert "defi_trade" in out or "approved" in out.lower()


@pytest.mark.asyncio
async def test_pending_and_approve_read_the_same_set(env):
    """Listing showed three queues; deciding read one. They are one function."""
    _seed_pending_self(env)
    _seed_tool_approval(env)

    listed = await act_on_inbound(_Agent(str(env)), _cmd("/pending", "/pending"))
    chooser = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve"))

    assert "2 pending" in listed
    assert "2 pending" in chooser


@pytest.mark.asyncio
async def test_the_disambiguation_list_is_tappable(env):
    """It used to print `  /approve <id>` — a client links only the verb, so the
    one tappable thing on screen was the half that does nothing."""
    _seed_pending_self(env)
    _seed_tool_approval(env)

    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve"))

    assert out.count("/approve_p_") == 2


@pytest.mark.asyncio
async def test_approve_all_decides_every_queue(env):
    """`/approve all` is advertised as "everything at once" and left two queues
    untouched."""
    from tools.controller.approval_queue import all_pending
    _seed_pending_self(env)
    board = _seed_tool_approval(env)

    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve all"))

    assert "2 approved" in out, out
    from core.instance import resolve_instance_id
    left = all_pending(user_id="alice", home_dir=str(env),
                       instance_id=resolve_instance_id(), board=board)
    assert left.items == []


@pytest.mark.asyncio
async def test_a_tap_decides_exactly_the_item_it_names(env):
    """The alias resolves through the LIVE queue, so it can only ever decide
    something that is genuinely waiting."""
    from core.instance import resolve_instance_id
    from core.self_evolution import list_pending, pending_tap_token
    _seed_pending_self(env)
    items = list_pending("alice", home_dir=str(env), instance_id=resolve_instance_id())
    token = pending_tap_token("approve", items[0])

    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", token))

    assert "failed" not in out.lower(), out
    assert list_pending("alice", home_dir=str(env),
                        instance_id=resolve_instance_id()) == []


@pytest.mark.asyncio
async def test_a_stale_alias_decides_nothing(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/approve", "/approve_p_ffffff"))
    assert "no pending proposal" in out.lower()


def test_the_tap_tokens_are_routable_commands():
    """A client sends `/approve_p_abc123` as its own token; the dispatcher must
    classify it as a COMMAND or it falls through to the model."""
    from core.surfaces.dispatcher import _COMMAND_SHAPE_RE
    assert _COMMAND_SHAPE_RE.match("/approve_p_abc123")
    assert _COMMAND_SHAPE_RE.match("/approve_all")
    assert "/approve" in _COMMANDS


# --- the plain word -----------------------------------------------------------

def test_a_plain_word_is_read_as_a_decision():
    from core.surfaces.owner_admin import parse_pending_decision

    assert parse_pending_decision("approve") == ("/approve", None)
    assert parse_pending_decision("reject") == ("/reject", None)
    assert parse_pending_decision("approve all") == ("/approve", "all")
    assert parse_pending_decision("yes please approve them all") == ("/approve", "all")


def test_a_sentence_is_not_a_decision():
    """The word has to be one the owner could only have meant as a decision."""
    from core.surfaces.owner_admin import parse_pending_decision

    for text in ("I approve of that plan",
                 "reject the third candidate token, it is a honeypot",
                 "approve and reject are both confusing",
                 "ok",            # the commonest filler there is
                 "yes",
                 ""):
        assert parse_pending_decision(text) is None, text


# --- one union, one decider, every seat ---------------------------------------

def test_every_seat_reads_the_one_union(env):
    """Telegram, the CLI and the REPL each built this join by hand; two of the
    three read only the first source when it came time to DECIDE."""
    import inspect

    from cli.commands import owner as cli_owner
    from cli.ui.commands import handlers as repl
    from surfaces.telegram import harness

    for src in (inspect.getsource(cli_owner._decide_all_and_echo),
                inspect.getsource(repl._h_pending),
                inspect.getsource(harness._pending_set)):
        assert "all_pending" in src or "decide_all_pending" in src


def test_the_repl_can_run_the_command_its_own_inbox_prints(env):
    """`REPL_REMEDIES` advertises `/pending approve tool_approval <id>`, and the
    handler used to hand that kind to the self-evolution promoter, which
    answered "unknown pending kind"."""
    from core.instance import resolve_instance_id
    from core.surfaces.inbox_render import REPL_REMEDIES
    from tools.controller.approval_queue import decide_pending
    board = _seed_tool_approval(env)
    ask_id = board.asks(user_id="alice")[0].id

    assert "tool_approval" in REPL_REMEDIES
    ok, msg = decide_pending("tool_approval", f"tap-{ask_id}", approve=True,
                             user_id="alice", home_dir=str(env),
                             instance_id=resolve_instance_id(), board=board)

    assert ok, msg
    assert "unknown pending kind" not in msg


def test_an_unreadable_source_is_named_not_dropped(env, monkeypatch):
    """A partial queue must report as partial. Silently dropping the source
    that held the owner's payment approval is the confident-zero class."""
    from core import self_evolution
    from tools.controller import approval_queue

    def _boom(*a, **kw):
        raise OSError("store is gone")

    monkeypatch.setattr(approval_queue, "list_pending_tool_approvals", _boom)
    _seed_pending_self(env)

    from core.instance import resolve_instance_id
    out = approval_queue.all_pending(user_id="alice", home_dir=str(env),
                                     instance_id=resolve_instance_id())

    assert len(out.items) == 1                    # the readable source survives
    assert out.unavailable == ["queued tool + spend approvals"]
    assert "could not read" in out.degraded_line()
