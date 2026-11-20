"""h_owner_reach.py — ``/claim`` ``/nft`` ``/dapp`` ``/contacts`` ``/identity`` in the REPL.

The five owner verbs the 2026-09-21 interface audit found to be AGENT-ONLY
(E6-E10): creator-fee claims, the NFT plane, armed dapp sessions, the
correspondent transcript, and ERC-8004 self-registration. Each is a thin
consumer of the SAME body Telegram runs (the money verbs and /dapp:
``tools.defi.owner_verbs``; the reads: ``core.surfaces.owner_reads``), so the
terminal and the phone cannot drift into two answers.

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
    from tools.defi.owner_verbs import claim
    ctx.emit(await _awaited(claim.claim_reply(_tenant(ctx), list(ctx.args or []), seat="repl")),
             title="claim")


async def h_pay(ctx) -> None:
    """068: pay for one x402 resource (price only unless ``go``)."""
    from tools.defi.owner_verbs import pay
    import uuid
    # 068 B9: one REPL command = one payment; the REPL never re-delivers.
    ctx.emit(await _awaited(pay.pay_reply(_tenant(ctx), list(ctx.args or []),
                                          request_id=f"repl:{uuid.uuid4().hex}", seat="repl")),
             title="pay")


async def h_nft(ctx) -> None:
    """Collectibles: list, info, transfer (always owner-approved), revoke an operator."""
    from tools.defi.owner_verbs import nft
    ctx.emit(await _awaited(nft.nft_reply(_tenant(ctx), list(ctx.args or []), seat="repl")),
             title="nft")


async def h_dapp(ctx) -> None:
    """Armed wallet-in-a-page sessions: list them, revoke one."""
    from tools.defi.owner_verbs import dapp
    ctx.emit(await _awaited(dapp.dapp_reply(_tenant(ctx), list(ctx.args or []))),
             title="dapp")


async def h_contacts(ctx) -> None:
    """What I said to a correspondent and what came back."""
    from core.surfaces.owner_reads import contacts_reply
    ctx.emit(await _awaited(contacts_reply(
        _tenant(ctx), _admin_data_dir(write=False), list(ctx.args or []),
        container=getattr(ctx, "container", None))), title="contacts")


async def h_thread(ctx) -> None:
    """061: our conversation across every session and rail."""
    from core.surfaces.owner_reads import history_reply
    ctx.emit(await _awaited(history_reply(
        _tenant(ctx), _admin_data_dir(write=False), list(ctx.args or []),
        container=getattr(ctx, "container", None))), title="thread")


async def h_why(ctx) -> None:
    """O10: the last gate refusals, the reason word for word."""
    from core.surfaces.owner_reads import why_reply
    ctx.emit(why_reply(_tenant(ctx), _admin_data_dir(write=False),
                               list(ctx.args or [])), title="why")


async def h_identity(ctx) -> None:
    """ERC-8004 self-registration: register (once) or republish the URI."""
    from tools.defi.owner_verbs import identity
    ctx.emit(await _awaited(identity.identity_reply(
        _tenant(ctx), _admin_data_dir(write=True), list(ctx.args or []), seat="repl")),
        title="identity")


def _usage(mod) -> str:
    u = getattr(mod, "USAGE", "")
    return u if isinstance(u, str) else "\n".join(u)


def register(reg, Command) -> None:
    """Register the five reach verbs. Called from ``h_a23.register``."""
    from core.surfaces.owner_reads import CONTACTS_USAGE, HISTORY_USAGE, WHY_USAGE
    from tools.defi.owner_verbs import dapp as dapp_ops
    from tools.defi.owner_verbs import claim as claim_ops
    from tools.defi.owner_verbs import identity as identity_ops
    from tools.defi.owner_verbs import nft as nft_ops
    from tools.defi.owner_verbs import pay as pay_ops
    reg.register(Command(
        "claim", h_claim, "Claim the creator fees a token I launched has earned",
        usage="<token> [go]", group="money", help_long=_usage(claim_ops),
        elsewhere="`/claim` on Telegram and `polyrob wallet claim`."))
    reg.register(Command(
        "pay", h_pay, "Pay for one x402 resource, up to a price you name",
        usage="<url> [max_usd] [go] [id=<name>]", group="money", help_long=_usage(pay_ops),
        elsewhere="`/pay` on Telegram."))
    reg.register(Command(
        "nft", h_nft, "Collectibles I hold: agent NFTs and their accounts, trust, send, list, info, transfer, revoke",
        usage="[send <id> <to> [go]]|list|info|transfer|revoke …", group="money", help_long=_usage(nft_ops),
        elsewhere="`/nft` on Telegram and `polyrob wallet nft`."))
    reg.register(Command(
        "dapp", h_dapp, "Wallet sessions armed inside a web page: list, revoke",
        usage="list|revoke <session-id>", group="money", help_long=_usage(dapp_ops),
        elsewhere="`/dapp` on Telegram and `polyrob wallet dapp`."))
    reg.register(Command(
        "contacts", h_contacts, "What I said to a correspondent and what came back",
        usage="[<surface> <address>]", group="look", help_long=CONTACTS_USAGE,
        elsewhere="`/contacts` on Telegram and `polyrob owner correspondents --history`."))
    reg.register(Command(
        "why", h_why, "The last things I refused to do, and the reason for each",
        usage="[n]", group="look", help_long=WHY_USAGE,
        elsewhere="`/why` on Telegram."))
    reg.register(Command(
        "thread", h_thread, "Our conversation across every session and rail, newest last",
        usage="[n | <hours>h]", group="remember", help_long=HISTORY_USAGE,
        elsewhere="`/thread` on Telegram."))
    reg.register(Command(
        "identity", h_identity, "Register my on-chain identity (ERC-8004) or republish its URI",
        usage="register|set-uri [on <chain>] [go]", group="display",
        help_long=_usage(identity_ops),
        elsewhere="`/identity` on Telegram and `polyrob identity register`."))
