"""/profile — which named profile (isolated home/identity) this run uses (W4)."""
from cli.ui.commands.registry import CommandContext


async def h_profile(ctx: CommandContext) -> None:
    try:
        import os

        from core.profiles import resolve_active_profile
        from core.runtime_paths import resolve_data_home
        sel = resolve_active_profile()
        if sel is None:
            lines = ["No active profile (default home).",
                     f"config home : {os.environ.get('POLYROB_HOME') or '~/.polyrob'}",
                     f"data home   : {resolve_data_home()}",
                     "Manage profiles with: polyrob profile create/list/use"]
        else:
            lines = [f"profile     : {sel.name} (selected via {sel.source})",
                     f"home        : {sel.home}",
                     f"data        : {sel.home / 'data'}"]
        ctx.emit("\n".join(lines), title="profile")
    except Exception as exc:
        ctx.emit(f"Could not resolve profile: {exc}", title="profile")
