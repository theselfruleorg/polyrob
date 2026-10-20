"""A REST or A2A tenant's own toolset carries no money tool (085 A6).

A tenant may name its own toolset on ``POST /api/task/sessions`` and on A2A
``message/send``. The wallet-authority hook (``tools/controller/wallet_authority.py``)
refuses every money action for a non-owner session user anyway, so a money tool
on a tenant session is only noise: drop it at the door. Every other tool passes
unchanged, and the bound owner keeps the list as given.

Lives in ``api/`` (the top tier): the owner check reads ``core.money.authority``,
and that import from ``core.money.classify`` would close an import cycle.
"""
from core.money.authority import owner_refusal
from core.money.classify import money_tool


def tenant_toolset(tools, user_id):
    """*tools* without the money tools unless *user_id* is the bound owner.

    Not a list (None, a malformed body) → returned as is."""
    if not isinstance(tools, (list, tuple)):
        return tools
    if owner_refusal(user_id) is None:
        return list(tools)
    return [t for t in tools if not money_tool(t)]
