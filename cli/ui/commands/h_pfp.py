"""/pfp — show/generate the agent avatar (Mindprint) from inside the REPL.

Generation stays OPTIONAL (owner decision 2026-07-14): nothing auto-creates the
avatar; this command is the discoverable way to do it without leaving the REPL.
Reuses the `polyrob pfp` CLI helpers — no new rendering abstractions (renderer
stack is frozen).
"""
from __future__ import annotations

from cli.ui.commands.registry import CommandContext


def h_pfp(ctx: CommandContext) -> None:
    args = [a for a in (ctx.args or []) if a]
    sub = (args[0].lower() if args else "status")

    from cli.commands import pfp as pfp_cli   # module import so tests can monkeypatch

    if sub in ("status", "info"):
        _status(ctx, pfp_cli)
    elif sub == "generate":
        _generate(ctx, pfp_cli, force=("force" in [a.lower().lstrip("-") for a in args[1:]]))
    elif sub == "show":
        _show(ctx, pfp_cli)
    else:
        ctx.emit("usage: /pfp [status|generate [force]|show]", title="pfp")


def _status(ctx, pfp_cli) -> None:
    from core.instance import load_pfp_meta, pfp_path
    home, instance_id = pfp_cli._instance_home()
    lines = [f"instance: {instance_id}"]
    png = pfp_path(home, instance_id) if home is not None else None
    if png is not None and png.is_file():
        meta = load_pfp_meta(home, instance_id) or {}
        lines.append(f"avatar: generated ({meta.get('rendered_by', 'unknown')}) → {png}")
        lines.append(f"seed: {meta.get('seed', '?')}  created: {meta.get('created_at', '?')}")
        lines.append("view: /pfp show · regenerate: /pfp generate force · console: /identity page")
    else:
        lines.append("avatar: not generated yet (optional)")
        lines.append("create it: /pfp generate   (or `polyrob pfp generate`)")
        lines.append("preview without saving: /pfp show")
    ctx.emit("\n".join(lines), title="pfp")


def _generate(ctx, pfp_cli, *, force: bool) -> None:
    from core.instance import pfp_path
    from modules.pfp import store
    from modules.pfp.renderer import PfpRenderUnavailable
    home, instance_id = pfp_cli._instance_home()
    if home is None:
        ctx.emit("could not resolve the data home", title="pfp")
        return
    png = pfp_path(home, instance_id)
    if png.is_file() and not force:
        ctx.emit(f"avatar already exists → {png}\nuse `/pfp generate force` to re-render",
                 title="pfp")
        return
    try:
        meta = store.generate_pfp(home, instance_id, force=force)
    except PfpRenderUnavailable as e:
        ctx.emit(f"cannot render and no reference PNG available: {e}\n"
                 "install the browser extra: pip install 'polyrob[browser]' && "
                 "playwright install chromium", title="pfp")
        return
    except Exception as e:  # fail-open — a REPL command must never crash the loop
        ctx.emit(f"avatar generation failed: {e}", title="pfp")
        return
    ctx.emit(f"avatar generated ({meta.get('rendered_by')}) → {png}", title="pfp")


def _show(ctx, pfp_cli) -> None:
    from cli.ui import terminal_render
    config = pfp_cli._resolve_config(None, None)
    if not terminal_render.supports_truecolor():
        ctx.emit("this terminal has no truecolor support — run `polyrob pfp show` "
                 "in a truecolor TTY instead", title="pfp")
        return
    frame = terminal_render.frame(config, width=40, still=True)
    out = ctx.console()
    if out is not None:
        try:
            from rich.text import Text
            out.print(Text.from_ansi(frame))
        except Exception:
            print(frame)
    else:
        print(frame)
    ctx.emit(terminal_render.text_line(config), title="pfp")
