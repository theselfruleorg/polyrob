"""h_money_verbs.py — ``/wallet`` ``/trade`` ``/bridge`` ``/dev`` in the REPL (043 A23).

Thin consumers of the SAME primitives the Telegram owner seat calls
(``surfaces.telegram.owner_ops`` and ``surfaces.telegram.dev_rail``), so the
terminal and the phone can never drift into different answers about a cap, a
quote, or what a bridge guarantees. This is the ``h_token.py`` / ``h_inbox.py``
pattern: a registrar beside its handlers, out of the size-ratcheted
``handlers.py``.

⚠️ REACH, never policy (043 global constraint). These verbs change no money
gate: ``tx_guard``, the per-transaction ceiling, the autonomous-vs-owner-queue
lane, and the 031 pause all still apply inside ``owner_ops``. ``/bridge``
QUOTES unless the owner adds ``go``; a spend above the autonomous ceiling still
waits in ``/pending``. ``/trade`` (``surfaces.telegram.trade_ops``) writes
nothing before its card's Confirm.

The data-home + tenant resolution reuses ``h_owner``'s canonical helpers — the
same home ``/config`` and ``/pending`` read — so a ``-P`` profile's wallet is
that profile's.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant
async def _awaited(value):
    """Return *value*, awaiting it when the helper is a coroutine.

    ⚠️ The ``surfaces.telegram`` reply helpers are migrating from sync to async
    one verb at a time (D12: a sync money helper blocked the event loop). A REPL
    consumer that hard-codes one shape breaks the moment its helper flips — and
    breaks SILENTLY, emitting the repr of a coroutine as if it were the answer.
    This shim makes the seat correct under both shapes.
    """
    import inspect
    return await value if inspect.isawaitable(value) else value



async def h_wallet(ctx) -> None:
    """Addresses, network, caps — and the one cap the owner may set from chat."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        await _awaited(owner_ops.wallet_reply(
            list(ctx.args or []), user_id=_tenant(ctx),
            data_dir=_admin_data_dir(write=None))),
        title="wallet",
    )


async def h_trade(ctx) -> None:
    """The trading mandate (086 W5) — the same ``trade_ops`` the phone runs."""
    import asyncio

    from core.surfaces.command_reply import reply_text
    from surfaces.telegram import trade_ops
    reply = await asyncio.to_thread(trade_ops.trade_reply, _tenant(ctx),
                                    _admin_data_dir(write=True), list(ctx.args or []),
                                    via="cli")
    ctx.emit(reply_text(reply), title="trade")


async def h_bridge(ctx) -> None:
    """Move NATIVE value between chains. Bare = quote; add ``go`` to execute."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        await _awaited(owner_ops.bridge_reply(
            _tenant(ctx), _admin_data_dir(write=True), list(ctx.args or []))),
        title="bridge",
    )


async def h_dev(ctx) -> None:
    """Owner ↔ on-host dev-loop rail (027). Raw free text, not re-joined args —
    the loop sees exactly what was typed. Async: ``perform_dev_command`` is a
    coroutine and the registry awaits an awaitable handler."""
    from surfaces.telegram.dev_rail import perform_dev_command, strip_dev_prefix
    # ctx.raw is the body without the leading slash (e.g. "dev pull main"); put
    # the slash back so strip_dev_prefix trims exactly "/dev".
    text = strip_dev_prefix("/" + (getattr(ctx, "raw", "") or "dev"))
    ctx.emit(await perform_dev_command(text), title="dev")


HELP_WALLET = (
    "  The agent wallet: addresses, active network, and the spend caps that\n"
    "  bound autonomous money. On-chain balances are a network read, so they\n"
    "  are opt-in — /wallet balances asks for them.\n"
    "\n"
    "  /wallet autonomous <usd> sets how much executes WITHOUT interrupting\n"
    "  you. It cannot widen maximum loss: the catastrophic per-transaction\n"
    "  ceiling still binds above it and stays env-only.\n"
    "\n"
    "  /wallet tokens lists the tokens I trust and why (canonical, our own\n"
    "  launch, owner approved, owner pin), what you marked not trusted, and\n"
    "  every quarantined or written-off holding. /wallet trust|untrust\n"
    "  <chain> <address> [go] records your word on one token.",
    "`/wallet` on Telegram and `polyrob wallet`.",
)

HELP_TRADE = (
    "  Trading: ONE decision holds the whole programme — the allocation, the\n"
    "  networks, the preset, the ticket size, how many open at once, and the\n"
    "  preset's exit and loss-stop numbers. Confirm starts it with its scheduled\n"
    "  jobs: a hunt (a $0 check wakes Rob only with a candidate and space for\n"
    "  an entry), an exit watch per open holding (started and ended by code),\n"
    "  and a daily review.\n"
    "\n"
    "    /trade start 150 solana,robinhood memecoin   the decision (nothing written yet)\n"
    "    /trade status          allocation, holdings, P&L, exit coverage, LLM cost\n"
    "    /trade pause           new entries off; exits keep running (/trade resume)\n"
    "    /trade stop            final; a decision offers 'close all'\n"
    "    /trade release         clear a latched loss stop\n"
    "\n"
    "  Nothing here raises a limit: the wallet's limits bind every trade.",
    "`/trade` on Telegram.",
)

HELP_BRIDGE = (
    "  Move NATIVE value between chains (SOL on solana, ETH on an EVM chain).\n"
    "  Bare form QUOTES — it asserts everything and broadcasts nothing. Add\n"
    "  'go' to execute: under your autonomous ceiling it runs and reports;\n"
    "  above it, the durable owner queue holds it — /inbox shows it and\n"
    "  /approve <id> releases it.\n"
    "\n"
    "    /bridge solana robinhood 0.9        quote only\n"
    "    /bridge solana robinhood 0.9 go     execute",
    "`/bridge` on Telegram and `polyrob wallet bridge`.",
)

HELP_DEV = (
    "  The owner ↔ on-host dev-loop rail (027): drive the maintenance loop\n"
    "  from where you are instead of SSH-ing to the box. Everything after\n"
    "  /dev is passed through verbatim, including newlines.\n"
    "\n"
    "  Gated by the dev rail's own flags; with it off you get an honest note,\n"
    "  never a stack trace.",
    "`/dev` on Telegram.",
)


def register(reg, Command) -> None:
    """Register the four money/dev verbs. Called from ``h_a23.register``."""
    reg.register(Command(
        "wallet", h_wallet,
        "Wallet addresses, network, caps — and the one cap you may set from chat",
        usage="[balances|tokens|trust|untrust|autonomous <usd>]", group="money",
        help_long=HELP_WALLET[0], elsewhere=HELP_WALLET[1],
    ))
    reg.register(Command(
        "trade", h_trade,
        "Trading: start it with one decision, see it, pause, resume, stop, release",
        usage="start|status|pause|resume|stop|release …", group="money",
        help_long=HELP_TRADE[0], elsewhere=HELP_TRADE[1],
    ))
    reg.register(Command(
        "bridge", h_bridge,
        "Move value between chains (quotes unless you add 'go')",
        usage="<from> <to> <amount> [from usdc] [as <asset>] [go]", group="money",
        help_long=HELP_BRIDGE[0], elsewhere=HELP_BRIDGE[1],
    ))
    reg.register(Command(
        "dev", h_dev,
        "Drive the on-host dev-loop rail (027) without leaving the terminal",
        usage="<free text>", group="control", raw_arguments=True,
        help_long=HELP_DEV[0], elsewhere=HELP_DEV[1],
    ))
