"""/pfp — the ONE-TIME avatar setup (Mindprint) from inside the REPL.

Generation stays OPTIONAL (owner decision 2026-07-14): nothing auto-creates the
avatar; this command is the discoverable way to do it without leaving the REPL.
Setup happens ONCE: `/pfp generate` mints a random DRAFT → `/pfp randomize
[face|voice]` re-rolls it → `/pfp keep` locks the identity permanently (after which
no verb can change it). Reuses the `polyrob pfp` CLI helpers — no new rendering
abstractions (renderer stack is frozen).
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
    elif sub in ("randomize", "shuffle", "reroll"):
        what = (args[1].lower() if len(args) > 1 else "all")
        if what not in ("all", "face", "voice"):
            ctx.emit("usage: /pfp randomize [face|voice]", title="pfp")
            return
        _randomize(ctx, pfp_cli, what)
    elif sub in ("keep", "accept", "freeze"):
        _keep(ctx, pfp_cli)
    elif sub in ("say", "speak", "voice"):
        _say(ctx, pfp_cli, " ".join(args[1:]).strip() or None)
    elif sub == "show":
        _show(ctx, pfp_cli)
    else:
        ctx.emit("usage: /pfp [status|generate|randomize [face|voice]|say [text]|keep|show]\n"
                 "setup happens once: generate a draft → randomize until it feels right "
                 "(see it, /pfp say to hear it) → keep it forever", title="pfp")


def _status(ctx, pfp_cli) -> None:
    from core.instance import load_pfp_meta, pfp_path
    home, instance_id = pfp_cli._instance_home()
    lines = [f"instance: {instance_id}"]
    png = pfp_path(home, instance_id) if home is not None else None
    if png is not None and png.is_file():
        meta = load_pfp_meta(home, instance_id) or {}
        tr = meta.get("traits") or {}
        v = meta.get("voice") or {}
        lines.append(f"avatar: generated ({meta.get('rendered_by', 'unknown')}) → {png}")
        lines.append(f"seed: {meta.get('seed', '?')}  variant: {meta.get('variant') or '(stock)'}  "
                     f"created: {meta.get('created_at', '?')}")
        if tr:
            lines.append(f"face:  {meta.get('seed_hex', '?')} · {tr.get('tier', '?')} · "
                         f"head {tr.get('head', '?')} · eyes {tr.get('eyes', '?')} · "
                         f"mouth {tr.get('mouth', '?')} · antenna {tr.get('antenna', '?')} · "
                         f"aura {tr.get('aura', '?')} · {tr.get('mode', '?')}")
        if v:
            lines.append(f"voice: pitch {v.get('pitch', '?')} · rate {v.get('rate', '?')} · "
                         f"timbre {v.get('timbre', '?')}")
        from modules.pfp.store import is_locked
        if is_locked(meta):
            lines.append("state: KEPT — the identity is permanent")
            lines.append("view: /pfp show · hear: /pfp say · push: `polyrob pfp push` · "
                         "console: /identity page")
        else:
            lines.append("state: DRAFT — setup is still open")
            lines.append("see: /pfp show · hear: /pfp say · re-roll: /pfp randomize [face|voice] · "
                         "keep it forever: /pfp keep")
    else:
        lines.append("avatar: not set up yet (optional, happens once)")
        lines.append("start setup with a random draft: /pfp generate   (or `polyrob pfp generate`)")
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
    config = pfp_cli.random_config()
    if png.is_file():
        from core.instance import load_pfp_meta
        from modules.pfp.config import load_frozen_config
        existing = load_pfp_meta(home, instance_id)
        if store.is_locked(existing):
            ctx.emit("the avatar identity is kept — setup happens once and it cannot "
                     "be changed afterwards.\nview: /pfp show · push: `polyrob pfp push`",
                     title="pfp")
            return
        if not force:
            ctx.emit(f"a DRAFT avatar already exists → {png}\nre-roll it: /pfp randomize · "
                     "keep it forever: /pfp keep", title="pfp")
            return
        try:  # force on a draft = pixels-only re-render of the SAME draft identity
            config = pfp_cli._core_config(load_frozen_config(existing))
        except Exception:
            pass
    try:
        meta = store.generate_pfp(home, instance_id, config=config, force=force)
    except PfpRenderUnavailable as e:
        ctx.emit(f"could not render the avatar: {e}\n"
                 "install the browser extra for the exact engine: pip install "
                 "'polyrob[browser]' && playwright install chromium", title="pfp")
        return
    except Exception as e:  # fail-open — a REPL command must never crash the loop
        ctx.emit(f"avatar generation failed: {e}", title="pfp")
        return
    _emit_rolled(ctx, pfp_cli, meta, png)


def _randomize(ctx, pfp_cli, what: str) -> None:
    from core.instance import pfp_path, load_pfp_meta
    from modules.pfp import store
    from modules.pfp.config import load_frozen_config, FrozenConfigError
    home, instance_id = pfp_cli._instance_home()
    if home is None:
        ctx.emit("could not resolve the data home", title="pfp")
        return
    meta = load_pfp_meta(home, instance_id)
    if meta is not None and store.is_locked(meta):
        ctx.emit("the avatar identity is kept — setup happens once; randomize is a "
                 "setup step, not a way to change a kept identity.\nview: /pfp show",
                 title="pfp")
        return
    try:
        current = pfp_cli._core_config(load_frozen_config(meta)) if meta \
            else pfp_cli.default_config()
    except FrozenConfigError:
        current = pfp_cli.default_config()
    if what == "voice":
        config = pfp_cli.shuffle_voice(current)
    elif what == "face":
        config = pfp_cli.shuffle_face(current)
    else:
        config = pfp_cli.shuffle_face(current)
        config["override"].pop("voice", None)
    try:
        new_meta = store.generate_pfp(home, instance_id, config=config, force=True)
    except Exception as e:  # fail-open — a REPL command must never crash the loop
        ctx.emit(f"avatar re-roll failed: {e}", title="pfp")
        return
    _emit_rolled(ctx, pfp_cli, new_meta, pfp_path(home, instance_id))


def _keep(ctx, pfp_cli) -> None:
    from core.instance import pfp_path
    from modules.pfp import store
    home, instance_id = pfp_cli._instance_home()
    if home is None:
        ctx.emit("could not resolve the data home", title="pfp")
        return
    try:
        meta = store.keep_pfp(home, instance_id)
    except FileNotFoundError:
        ctx.emit("no avatar to keep — start setup with /pfp generate", title="pfp")
        return
    except Exception as e:  # fail-open — a REPL command must never crash the loop
        ctx.emit(f"keep failed: {e}", title="pfp")
        return
    try:
        _render_face(ctx, pfp_cli._core_config(meta))
    except Exception:
        pass
    ctx.emit(f"identity kept — this is {instance_id!r}'s face + voice, permanently.\n"
             + pfp_cli.frozen_report(meta, pfp_path(home, instance_id)), title="pfp")


def _render_face(ctx, config) -> bool:
    """Print the truecolor face frame into the REPL console. False if unsupported."""
    from cli.ui import terminal_render
    if not terminal_render.supports_truecolor():
        return False
    try:
        frame = terminal_render.frame(config, width=40, still=True)
    except Exception:
        return False
    out = ctx.console()
    if out is not None:
        try:
            from rich.text import Text
            out.print(Text.from_ansi(frame))
            return True
        except Exception:
            pass
    print(frame)
    return True


def _emit_rolled(ctx, pfp_cli, meta, png) -> None:
    """SEE what was rolled: face frame (when the terminal can) + the identity report."""
    try:
        _render_face(ctx, pfp_cli._core_config(meta))
    except Exception:
        pass
    ctx.emit(pfp_cli.frozen_report(meta, png, regenerated=True), title="pfp")


def _say(ctx, pfp_cli, text) -> None:
    from modules.pfp.voice import speak_voice, VoiceUnavailable, DEFAULT_TEXT
    from modules.pfp.mesh import Mesh
    from core.instance import load_pfp_meta
    home, instance_id = pfp_cli._instance_home()
    meta = load_pfp_meta(home, instance_id) if home is not None else None
    if meta and isinstance(meta.get("voice"), dict):
        voice = meta["voice"]
    else:
        voice = Mesh(pfp_cli._resolve_config(None, None)).voice()   # deferrable preview
    try:
        engine = speak_voice(voice, text or DEFAULT_TEXT)
    except VoiceUnavailable as e:
        ctx.emit(str(e), title="pfp")
        return
    except Exception as e:  # fail-open — a REPL command must never crash the loop
        ctx.emit(f"could not speak: {e}", title="pfp")
        return
    ctx.emit(f"voice: pitch {voice.get('pitch', '?')} · rate {voice.get('rate', '?')} · "
             f"timbre {voice.get('timbre', '?')}   (spoken via {engine})", title="pfp")


def _show(ctx, pfp_cli) -> None:
    from cli.ui import terminal_render
    config = pfp_cli._resolve_config(None, None)
    if not _render_face(ctx, config):
        ctx.emit("this terminal has no truecolor support — run `polyrob pfp show` "
                 "in a truecolor TTY instead", title="pfp")
        return
    ctx.emit(terminal_render.text_line(config), title="pfp")
