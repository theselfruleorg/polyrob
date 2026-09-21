"""`polyrob identity` — one seat for the instance's identity surfaces (043 A14/A25).

Mounts the existing, unmodified `soul`, `persona`, and `pfp` command groups
under `identity soul` / `identity persona` / `identity avatar`. This is a
routing seam only — each mounted group keeps its own callback, options, and
subcommands. `polyrob soul`, `polyrob persona`, and `polyrob pfp` remain
directly invocable at their historic top-level names forever (043 §4.1: an
alias invokes forever, it is never removed) — they are only hidden from the
grouped `--help` listing (`cli/polyrob.py::_COMMAND_ALIASES`).
"""
from __future__ import annotations

import click


@click.group("identity")
def identity():
    """The instance's identity: SOUL, persona (voice), avatar, and on-chain."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _owner_ctx():
    """The CLI IS the owner seat — there is no forged-turn context to pass.

    Imported from ``cli.commands.wallet`` rather than rebuilt: the tenant an
    owner money verb acts under is ONE rule (``admin_owner_principal``, which
    reads the DEPLOYED declaration an SSH shell does not carry), and a second
    copy here would let a registration's spend land in a bucket this box's own
    ledger never reads.
    """
    from cli.commands.wallet import _owner_ctx as _shared
    return _shared()


def _echo_result(result, *, what: str) -> None:
    if getattr(result, "error", None):
        raise click.ClickException(result.error)
    # ⚠️ ActionResult's field is `extracted_content`, NOT `content` — reading
    # `.content` printed "(no output)" over a complete report on its first
    # prod run (the recorded `wallet bridge` defect).
    body = getattr(result, "extracted_content", None)
    if not body:
        raise click.ClickException(
            f"the {what} returned neither an error nor a report — that is a "
            f"bug, not an empty result; assume nothing about what happened.")
    click.echo(body)


@identity.command("register")
@click.option("--chain", default="base", show_default=True,
              help="Chain whose ERC-8004 Identity Registry to mint on. The "
                   "registries are PINNED; an unsupported chain refuses.")
@click.option("--max-usd", type=float, default=25.0, show_default=True,
              help="Ceiling for the transaction FEE. A registration sends "
                   "nothing else.")
@click.option("--dry-run/--execute", "dry_run", default=True, show_default=True,
              help="Default simulates and reports; --execute broadcasts.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the typed confirmation.")
def identity_register(chain, max_usd, dry_run, yes):
    """Register THIS instance on the ERC-8004 Identity Registry.

    E8: this existed only as an agent action, so the one identity an owner
    might most want to mint deliberately had no owner seat.

    ⚠️ `register()` is NOT idempotent — a second call mints a second token and
    leaves two agentIds with no authority between them. The verb reads the
    CHAIN for an existing token before it signs (never a local flag, which a
    fresh data dir would lose), and a FAILED read refuses rather than reading
    as "not registered". This wrapper adds no second gate: it hands the same
    params to the same method the agent calls.
    """
    import asyncio

    from tools.defi.trade_tool import DefiTradeTool, RegisterAgentParams

    if not dry_run and not yes:
        click.echo(click.style(
            f"About to mint this instance's ERC-8004 identity on {chain}. "
            f"A half-written identity on-chain is permanent, and a second "
            f"registration cannot be undone.", fg="yellow"))
        click.confirm("Proceed?", abort=True)
    result = asyncio.run(DefiTradeTool().register_agent(
        RegisterAgentParams(chain=chain, max_spend_usd=max_usd,
                            dry_run=dry_run), _owner_ctx()))
    _echo_result(result, what="registration")


@identity.command("set-uri")
@click.argument("agent_id", type=int)
@click.option("--chain", default="base", show_default=True)
@click.option("--max-usd", type=float, default=10.0, show_default=True)
@click.option("--dry-run/--execute", "dry_run", default=True, show_default=True,
              help="Default simulates and reports; --execute broadcasts.")
def identity_set_uri(agent_id, chain, max_usd, dry_run):
    """Update the published registration document for an EXISTING agentId.

    This mints nothing. AGENT_ID is the tokenId `identity register` proved.
    """
    import asyncio

    from tools.defi.trade_tool import DefiTradeTool, SetAgentUriParams

    result = asyncio.run(DefiTradeTool().set_agent_uri(
        SetAgentUriParams(chain=chain, agent_id=agent_id,
                          max_spend_usd=max_usd, dry_run=dry_run),
        _owner_ctx()))
    _echo_result(result, what="URI update")


def _mount() -> None:
    # Imported inside the factory (not at module top) so loading this module
    # does no more work than loading `soul`/`persona`/`pfp` already did on
    # their own — the lazy-loading contract in cli/polyrob.py (_LAZY_SUBCOMMANDS)
    # stays honest either way.
    from cli.commands.persona import persona
    from cli.commands.pfp import pfp
    from cli.commands.soul import soul

    identity.add_command(soul, name="soul")
    identity.add_command(persona, name="persona")
    identity.add_command(pfp, name="avatar")


_mount()
