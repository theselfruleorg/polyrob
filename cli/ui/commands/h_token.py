"""`/launch` and `/deploy` in the REPL (042b).

Thin consumers of ``surfaces.telegram.token_ops`` — the SAME helpers the chat
seat renders, so the two surfaces can never drift into different answers about
what a launch costs or what a deploy guarantees. That is the pattern
``core.mcp_admin`` established and the reason `/mcp` reads identically
everywhere.

⚠️ Both QUOTE by default. `go` is a deliberate second act, and it is not the
approval: the durable owner queue still applies above the autonomous ceiling and
every guard applies underneath.
"""


def _render(ctx, text: str, *, title: str) -> None:
    ctx.emit(text, title=title)


def _uid(ctx) -> str:
    """The REPL session's tenant — ONE rule, imported (C75).

    This module carried three byte-identical copies of
    ``(ctx.user_id or "").strip() or "local"``; ``h_owner._tenant`` is the
    canonical one every other REPL money verb already calls, so a change to
    what "this tenant" means cannot reach two of the three.
    """
    from cli.ui.commands.h_owner import _tenant
    return _tenant(ctx)


async def h_launch(ctx) -> None:
    """REPL handler: /launch <SYMBOL> <name…> [buy <amount>] [go]."""
    from surfaces.telegram import token_ops
    from cli.ui.commands.h_money_verbs import _awaited

    args = list(getattr(ctx, "args", None) or [])
    _render(ctx, await _awaited(token_ops.launch_reply(_uid(ctx), args)),
            title="launch")


async def h_deploy(ctx) -> None:
    """REPL handler: /deploy <SYMBOL> <supply> <name…> [on <chain>] [go]."""
    from surfaces.telegram import token_ops
    from cli.ui.commands.h_money_verbs import _awaited

    args = list(getattr(ctx, "args", None) or [])
    _render(ctx, await _awaited(token_ops.deploy_reply(_uid(ctx), args)),
            title="deploy")


async def h_lp(ctx):
    from surfaces.telegram.lp_ops import lp_reply
    from cli.ui.commands.h_money_verbs import _awaited
    _render(ctx, await _awaited(lp_reply(_uid(ctx),
                                         list(getattr(ctx, "args", None) or []))),
            title="liquidity")


def register(reg, Command) -> None:
    """Register both verbs.

    Lives HERE rather than in ``handlers.py`` because that file is at its
    size ratchet, whose whole instruction is to extract new behaviour into a new
    module instead of growing it. A registrar beside its handlers is also the
    shape the next verb should copy.
    """
    reg.register(
        Command("launch", h_launch,
                "Launch a token on the Pons launchpad (quotes unless you add 'go')",
                usage="<SYMBOL> <name…> [buy <amount>] [go]", group="money")
    )
    reg.register(
        Command("deploy", h_deploy,
                "Deploy a fixed-supply token (quotes unless you add 'go')",
                usage="<SYMBOL> <supply> <name…> [on <chain>] [vanity <hex>] [go]", group="money")
    )

    reg.register(Command("lp", h_lp, "Uniswap liquidity (dry-run unless go)",
                         usage="positions|pool|quote|add|remove|collect", group="money"))
