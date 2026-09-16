"""h_money_verbs.py — ``/wallet`` ``/trade`` ``/bridge`` ``/dev`` in the REPL (043 A23).

Thin consumers of the SAME primitives the Telegram owner seat calls
(``surfaces.telegram.owner_ops`` and ``surfaces.telegram.dev_rail``), so the
terminal and the phone can never drift into different answers about a cap, a
quote, or what a bridge guarantees. This is the ``h_token.py`` / ``h_inbox.py``
pattern: a registrar beside its handlers, out of the size-ratcheted
``handlers.py``.

⚠️ REACH, never policy (043 global constraint). These verbs change no money
gate: ``tx_guard``, the per-transaction ceiling, the autonomous-vs-owner-queue
lane, and the 031 pause all still apply inside ``owner_ops``. ``/trade`` and
``/bridge`` QUOTE unless the owner adds ``go``; a spend above the autonomous
ceiling still waits in ``/pending``.

The data-home + tenant resolution reuses ``h_owner``'s canonical helpers — the
same home ``/config`` and ``/pending`` read — so a ``-P`` profile's wallet is
that profile's.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


def h_wallet(ctx) -> None:
    """Addresses, network, caps — and the one cap the owner may set from chat."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        owner_ops.wallet_reply(list(ctx.args or []),
                               user_id=_tenant(ctx), data_dir=_admin_data_dir(ctx)),
        title="wallet",
    )


def h_trade(ctx) -> None:
    """Seed a run that carries the money verb — the owner asking IS the grant."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        owner_ops.trade_reply(_tenant(ctx), _admin_data_dir(ctx), list(ctx.args or [])),
        title="trade",
    )


def h_bridge(ctx) -> None:
    """Move NATIVE value between chains. Bare = quote; add ``go`` to execute."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        owner_ops.bridge_reply(_tenant(ctx), _admin_data_dir(ctx), list(ctx.args or [])),
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
    "  ceiling still binds above it and stays env-only.",
    "`/wallet` on Telegram and `polyrob wallet`.",
)

HELP_TRADE = (
    "  Seed a run that actually carries the money verb. Everything the agent\n"
    "  writes for ITSELF has money stripped — correctly — so 'bridge my SOL'\n"
    "  produced a goal that could never execute. Your asking, from this seat,\n"
    "  IS the authorization a self-written goal cannot express.\n"
    "\n"
    "  Nothing here widens a cap. Every spend is still bounded by the\n"
    "  per-transaction ceiling and still queues for /approve above the\n"
    "  autonomous ceiling.",
    "`/trade` on Telegram.",
)

HELP_BRIDGE = (
    "  Move NATIVE value between chains (SOL on solana, ETH on an EVM chain).\n"
    "  Bare form QUOTES — it asserts everything and broadcasts nothing. Add\n"
    "  'go' to execute: under your autonomous ceiling it runs and reports;\n"
    "  above it, the durable owner queue holds it and you decide in /pending.\n"
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
        usage="[balances|autonomous <usd>]", group="money",
        help_long=HELP_WALLET[0], elsewhere=HELP_WALLET[1],
    ))
    reg.register(Command(
        "trade", h_trade,
        "Seed a run that carries the money verb (your asking is the grant)",
        usage="<what to do>", group="money", raw_arguments=True,
        help_long=HELP_TRADE[0], elsewhere=HELP_TRADE[1],
    ))
    reg.register(Command(
        "bridge", h_bridge,
        "Move native value between chains (quotes unless you add 'go')",
        usage="<from> <to> <amount> [go]", group="money",
        help_long=HELP_BRIDGE[0], elsewhere=HELP_BRIDGE[1],
    ))
    reg.register(Command(
        "dev", h_dev,
        "Drive the on-host dev-loop rail (027) without leaving the terminal",
        usage="<free text>", group="work", raw_arguments=True,
        help_long=HELP_DEV[0], elsewhere=HELP_DEV[1],
    ))
