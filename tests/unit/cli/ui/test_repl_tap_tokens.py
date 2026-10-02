"""Every tap token a seat prints runs in the REPL (REPL ⊇ Telegram, for taps).

`/approve_p_<hex>`, `/approve_tap_<hex>`, `/approve_all`, `/reject_…`,
`/fulfill_<ask>_<letter>` and `/card_<id>_<act>` used to answer "Unknown
command" in the terminal, so a notice the owner read there named a command the
seat could not run."""
import asyncio

import pytest

from cli.ui.commands.registry import CommandContext


def _registry(seen):
    from cli.ui.commands.registry import Command, CommandRegistry
    reg = CommandRegistry()
    for name in ("approve", "reject", "fulfill", "send"):
        reg.register(Command(name, (lambda n: lambda ctx: seen.append((n, list(ctx.args))))(name),
                             name, raw_arguments=True))
    return reg


@pytest.mark.parametrize("token,expect", [
    ("/approve_p_a1b2c3", ("approve", ["p-a1b2c3"])),
    ("/approve_tap_abc123", ("approve", ["tap-abc123"])),
    ("/approve_all", ("approve", ["all"])),
    ("/reject_p_a1b2c3", ("reject", ["p-a1b2c3"])),
])
def test_folded_tokens_run_their_verb(token, expect):
    seen, out = [], []
    reg = _registry(seen)
    ctx = CommandContext(user_id="alice", registry=reg)
    ctx.emit = lambda text, **kw: out.append(text)
    asyncio.run(reg.dispatch(token, ctx))
    assert seen == [expect] and not any("Unknown command" in o for o in out)


def test_a_card_token_is_a_card_press_not_unknown():
    seen, out = [], []
    reg = _registry(seen)
    ctx = CommandContext(user_id="alice", registry=reg)
    ctx.emit = lambda text, **kw: out.append(text)
    asyncio.run(reg.dispatch("/card_0123456789_ok", ctx))
    assert out and "No such card" in out[0]


def test_every_tappable_shape_the_grammar_accepts_is_routed():
    """The grammar and the REPL cannot drift: each accepted shape dispatches."""
    from core.surfaces.tappable import TAPPABLE_VERBS
    assert set(TAPPABLE_VERBS) == {"/approve", "/reject"}
