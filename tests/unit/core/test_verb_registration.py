"""067 P5a: owner verbs a pack contributes (core.verbs.register_verbs)."""
import pytest

import core.verbs as V
from core.verbs import Verb

MONEY = ("/book", "/wallet", "/invoices", "/settle", "/trade", "/bridge", "/launch",
         "/deploy", "/lp", "/claim", "/nft", "/dapp", "/paid", "/pay")
#: W1: a holding's lifecycle — contributed WITH Telegram + REPL handlers.
HOLDINGS = ("/writeoff", "/unquarantine")
SEND = ("/send",)
#: 2026-09-27: /swap, contributed WITH Telegram + REPL handlers like /send.
SWAP = ("/swap",)
CHECK = ("/check",)  # 071: any address or ticker, read-only
#: 2026-10-08: /adopt (core.standing_verbs) — not money, same contributed shape.
ADOPT = ("/adopt",)


@pytest.fixture
def scratch(monkeypatch):
    """Registration state restored after the test (the union views too)."""
    monkeypatch.setattr(V, "_REGISTERED", list(V._REGISTERED))
    monkeypatch.setattr(V, "_HANDLERS", dict(V._HANDLERS))
    monkeypatch.setattr(V, "_ROOM_OK", set(V._ROOM_OK))
    yield V
    monkeypatch.undo()
    V._rebuild()


def test_the_money_verbs_are_contributed_not_core_rows():
    assert not any(v.group == "money" for v in V._CORE_TABLE)
    assert tuple(v.name for v in V.registered_verbs("core.money_verbs")) == MONEY
    names = [v.name for v in V.VERB_TABLE]
    # the union keeps them in the money section, in their old order
    assert names[names.index("/book"):names.index("/book") + len(MONEY)] == list(MONEY)
    # A pack's rows (the x pack's `/x`) register in phase 2 and are routed too.
    assert V.routed_names() - V.pack_verb_names() == (
        frozenset(MONEY) | frozenset(HOLDINGS) | frozenset(SEND) | frozenset(SWAP)
        | frozenset(CHECK) | frozenset(ADOPT))


def test_no_money_verb_carries_a_handler_yet():
    """P5a: every seat still runs them from its own branch (P5b moves them)."""
    assert not any(V.handler_ref(seat, n) for seat in V.KNOWN_SEATS for n in MONEY)


def test_room_safety_is_what_telegram_refused_before():
    from surfaces.telegram.harness import _ROOM_REFUSED_COMMANDS
    for name in MONEY:
        assert V.room_refused(name) == (name not in ("/paid", "/book")), name
        # the seat literal still agrees (P5b deletes the money names from it)
        assert (name in _ROOM_REFUSED_COMMANDS) == V.room_refused(name), name


@pytest.mark.parametrize("row, err", [
    (Verb("/x1", "nowhere", "help"), "GROUP_ORDER"),
    (Verb("/status", "look", "help"), "already taken"),
    (Verb("/wallet", "money", "help"), "already taken"),
    (Verb("x1", "look", "help"), "/name"),
    (Verb("/x1", "look", " "), "/name"),
])
def test_bad_rows_are_refused(scratch, row, err):
    with pytest.raises(ValueError, match=err):
        scratch.register_verbs([row], source="pack:t")


def test_bad_handlers_are_refused_and_nothing_registers(scratch):
    row = Verb("/xtest", "work", "a test verb")
    for handlers, err in [({"fax": {"/xtest": "m:f"}}, "unknown seat"),
                          ({"repl": {"/other": "m:f"}}, "not one of the rows"),
                          ({"repl": {"/xtest": "nocolon"}}, "module:attr")]:
        with pytest.raises(ValueError, match=err):
            scratch.register_verbs([row], handlers, source="pack:t")
    with pytest.raises(ValueError, match="room verbs"):
        scratch.register_verbs([row], room_verbs=("/nope",), source="pack:t")
    assert V.verb_for("/xtest") is None


def test_a_contributed_verb_joins_every_view(scratch):
    row = Verb("/xtest", "work", "a test verb")
    scratch.register_verbs([row], {"repl": {"/xtest": "tests.unit.core.test_verb_registration:_repl"},
                                   "telegram": {"/xtest": "m:f"}}, source="pack:t")
    scratch.register_verbs([row], source="pack:t")          # idempotent
    assert V.verb_for("/xtest") is row
    assert row in dict(V.grouped("telegram"))["work"]
    assert V.handler_ref("repl", "/xtest").endswith(":_repl")
    from core.surfaces.dispatcher import RouteKind, command_names
    assert "/xtest" in command_names()
    assert V.room_refused("/xtest")
    from core.owner_remedy import chat_verbs
    assert "/xtest" in chat_verbs()
    del RouteKind


def _repl(ctx):
    return "ran"


def test_the_repl_registers_a_contributed_handler(scratch):
    scratch.register_verbs([Verb("/xtest", "work", "a test verb")],
                           {"repl": {"/xtest": "tests.unit.core.test_verb_registration:_repl"}},
                           source="pack:t")
    from cli.ui.commands.handlers import build_default_registry
    cmd = build_default_registry().lookup("xtest")
    assert cmd is not None and cmd.handler is _repl and cmd.group == "work"


def test_an_empty_money_group_renders_the_install_hint(scratch):
    scratch._REGISTERED[:] = []
    scratch._rebuild()
    assert V.empty_group_hints("telegram") == [("money", V.EMPTY_GROUP_HINTS["money"])]
    from surfaces.telegram.harness import _build_help_body
    body = _build_help_body()
    assert "— Money —\n  No money verbs: install the wallet pack" in body
    assert "/wallet" not in body


@pytest.mark.asyncio
async def test_telegram_runs_a_contributed_handler():
    from surfaces.telegram.harness import _run_contributed
    out = await _run_contributed("tests.unit.core.test_verb_registration:_tg",
                                 user_id="u", data_dir="d", args=["a"])
    assert out == "u d ['a']"


async def _tg(*, user_id, data_dir, args):
    return f"{user_id} {data_dir} {args}"


def test_the_holdings_verbs_carry_seat_handlers_and_are_refused_in_a_room():
    assert tuple(v.name for v in V.registered_verbs("core.money_verbs.holdings")) == HOLDINGS
    for name in HOLDINGS:
        assert V.verb_for(name).group == "money"
        assert V.handler_ref("telegram", name).startswith("surfaces.telegram.holdings_ops:")
        assert V.handler_ref("repl", name).startswith("cli.ui.commands.h_holdings:")
        assert V.room_refused(name)


def test_the_repl_runs_the_holdings_verbs():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    for name in ("writeoff", "unquarantine"):
        cmd = reg.lookup(name)
        assert cmd is not None and cmd.group == "money", name
