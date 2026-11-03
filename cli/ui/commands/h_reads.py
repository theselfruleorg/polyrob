"""h_reads.py — ``/more`` and ``/rules`` in the REPL (096 W5).

The twins of the Telegram read verbs: the same helpers
(``core.surfaces.more_store.more_reply``,
``surfaces.telegram.owner_ops.rules_reply``), so the terminal and the phone
answer one verb with one text. Both are READS — neither decides anything.

``/more`` reads the continuation store of THIS process: a ``/more_<token>``
printed by the deployed service names text kept in that process, so here it
answers "gone" and names the verbs that render the list again.
"""
from __future__ import annotations


def h_more(ctx) -> None:
    """The rest of a list or text a reply cut short."""
    from cli.ui.commands.h_owner import _tenant
    from core.surfaces.more_store import more_reply
    ctx.emit(more_reply(_tenant(ctx), list(ctx.args or [])), title="more")


def h_rules(ctx) -> None:
    """The owner rules and the operating contract, numbered, read only."""
    from cli.ui.commands.h_owner import _admin_data_dir, _tenant
    from core.instance import resolve_instance_id
    from surfaces.telegram.owner_ops import rules_reply
    ctx.emit(rules_reply(_tenant(ctx), _admin_data_dir(write=False),
                         resolve_instance_id()), title="rules")


def register(reg, command_cls) -> None:
    """Add both verbs to *reg*. Called from ``handlers.build_default_registry``."""
    reg.register(command_cls(
        "more", h_more, "The rest of a list or text I cut short",
        usage="<token>", group="look"))
    reg.register(command_cls(
        "rules", h_rules, "Your rules for me and my operating contract, numbered",
        group="remember"))
