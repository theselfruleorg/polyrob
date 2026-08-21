"""/profile — which named profile (isolated home/identity) this run uses (W4)."""
from cli.ui.commands.registry import CommandContext


async def h_profile(ctx: CommandContext) -> None:
    try:
        import os

        from core.profiles import resolve_active_profile
        sel = resolve_active_profile()
        if sel is None:
            lines = ["No active profile (legacy/project mode).",
                     f"config home : {os.environ.get('POLYROB_HOME') or '~/.polyrob'}",
                     f"data home   : {os.environ.get('POLYROB_DATA_DIR') or './.polyrob'}",
                     "Manage profiles with: polyrob profile create/list/use"]
        else:
            lines = [f"profile     : {sel.name} (selected via {sel.source})",
                     f"home        : {sel.home}",
                     f"data        : {sel.home / 'data'}"]
        ctx.emit("\n".join(lines), title="profile")
    except Exception as exc:
        ctx.emit(f"Could not resolve profile: {exc}", title="profile")
