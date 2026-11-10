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


def default_session_tools(user_id) -> list:
    """What a session gets when it asked for nothing. 081 WS-3.1: the console is an owner
    seat. 104 D2: on the owner's own computer the owner starts with the terminal's tools;
    any other caller keeps the narrow default."""
    from agents.task.tool_defaults import cli_default_tools, owner_seat_tool_ids
    from core.config_policy import local_mode_enabled
    from core.instance import is_owner_uid
    from tools.controller.approval import is_console_process
    if is_console_process() and local_mode_enabled() and is_owner_uid(user_id):
        return cli_default_tools()
    return owner_seat_tool_ids(user_id) or ["browser", "filesystem"]


def refuse_terminal_chat(session_info, session_id: str) -> None:
    """104 D1: a console process never steps a chat the terminal runs, on any route."""
    from tools.controller.approval import is_console_process
    creator = (session_info or {}).get("creator") if isinstance(session_info, dict) else None
    if is_console_process() and creator == "cli":
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=(
            f'This chat runs in the terminal. To continue it: polyrob run --resume {session_id} "your message".'))
