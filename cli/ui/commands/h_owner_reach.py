"""h_owner_reach.py — ``/claim`` ``/nft`` ``/dapp`` ``/contacts`` ``/identity`` in the REPL.

The five owner verbs the 2026-09-21 interface audit found to be AGENT-ONLY
(E6-E10): creator-fee claims, the NFT plane, armed dapp sessions, the
correspondent transcript, and ERC-8004 self-registration. Each is a thin
consumer of the SAME helper Telegram calls (``surfaces.telegram.*_ops``), so
the terminal and the phone cannot drift into two answers.

⚠️ REACH, never policy: every money gate (``tx_guard``, the caps, the
always-owner-approved NFT transfer, the 031 pause) applies inside the helper.
A verb quotes/dry-runs unless the owner adds ``go``.
"""
from __future__ import annotations

import inspect

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


async def _awaited(value):
    return await value if inspect.isawaitable(value) else value


async def h_claim(ctx) -> None:
    """Claim the creator fees a launched token earned (dry-run unless ``go``)."""
    from surfaces.telegram import claim_ops
    ctx.emit(await _awaited(claim_ops.claim_reply(_tenant(ctx), list(ctx.args or []))),
             title="claim")


async def h_nft(ctx) -> None:
    """Collectibles: list, info, transfer (always owner-approved), revoke an operator."""
    from surfaces.telegram import nft_ops
    ctx.emit(await _awaited(nft_ops.nft_reply(_tenant(ctx), list(ctx.args or []))),
             title="nft")


async def h_dapp(ctx) -> None:
    """Armed wallet-in-a-page sessions: list them, revoke one."""
    from surfaces.telegram import dapp_ops
    ctx.emit(await _awaited(dapp_ops.dapp_reply(_tenant(ctx), list(ctx.args or []))),
             title="dapp")


async def h_contacts(ctx) -> None:
    """What I said to a correspondent and what came back."""
    from surfaces.telegram import contacts_ops
    ctx.emit(await _awaited(contacts_ops.contacts_reply(
        _tenant(ctx), _admin_data_dir(write=False), list(ctx.args or []),
        container=getattr(ctx, "container", None))), title="contacts")


async def h_identity(ctx) -> None:
    """ERC-8004 self-registration: register (once) or republish the URI."""
    from surfaces.telegram import identity_ops
    ctx.emit(await _awaited(identity_ops.identity_reply(
        _tenant(ctx), _admin_data_dir(write=True), list(ctx.args or []))),
        title="identity")


def _usage(mod) -> str:
    u = getattr(mod, "USAGE", "")
    return u if isinstance(u, str) else "\n".join(u)


def register(reg, Command) -> None:
    """Register the five reach verbs. Called from ``h_a23.register``."""
    from surfaces.telegram import claim_ops, contacts_ops, dapp_ops, identity_ops, nft_ops
    reg.register(Command(
        "claim", h_claim, "Claim the creator fees a token I launched has earned",
        usage="<token> [go]", group="money", help_long=_usage(claim_ops),
        elsewhere="`/claim` on Telegram and `polyrob wallet claim`."))
    reg.register(Command(
        "nft", h_nft, "Collectibles I hold: list, info, transfer, revoke an operator",
        usage="list|info|transfer|revoke …", group="money", help_long=_usage(nft_ops),
        elsewhere="`/nft` on Telegram and `polyrob wallet nft`."))
    reg.register(Command(
        "dapp", h_dapp, "Wallet sessions armed inside a web page: list, revoke",
        usage="list|revoke <session-id>", group="money", help_long=_usage(dapp_ops),
        elsewhere="`/dapp` on Telegram and `polyrob wallet dapp`."))
    reg.register(Command(
        "contacts", h_contacts, "What I said to a correspondent and what came back",
        usage="[<surface> <address>]", group="look", help_long=_usage(contacts_ops),
        elsewhere="`/contacts` on Telegram and `polyrob owner correspondents --history`."))
    reg.register(Command(
        "identity", h_identity, "Register my on-chain identity (ERC-8004) or republish its URI",
        usage="register|set-uri [on <chain>] [go]", group="display",
        help_long=_usage(identity_ops),
        elsewhere="`/identity` on Telegram and `polyrob identity register`."))
