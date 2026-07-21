"""`polyrob pfp` — the agent avatar (Mindprint) command group.

Renders the agent's face LIVE in the terminal from the engine field port (no PNG).
Avatar creation is optional/deferrable: `pfp show` always works, even with no stored
avatar (it renders from a seed).

**Setup happens ONCE.** `pfp generate` mints a RANDOM DRAFT identity (each instance
gets its own face + voice); while it is a draft, `pfp randomize [face|voice]` re-rolls
it. `pfp keep` (or `pfp pick`'s save) accepts the identity and locks it PERMANENTLY —
after that no verb can change it (the deliberate escape hatch is deleting the instance
`pfp/` directory by hand). Everything freezes into the instance identity home
(`identity/{instance_id}/pfp/`), which every consumer (webview /identity, invoice
cards, `pfp push`) reads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import click

from modules.pfp.config import load_frozen_config, FrozenConfigError
from modules.pfp.identity import (
    DEFAULT_SEED as _DEFAULT_SEED,
    b36_variant as _b36_variant,
    core_config as _core_config,
    default_config,
    random_config,
    shuffle_face,
    shuffle_voice,
)
from cli.ui import terminal_render

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _ROOT / "avatar" / "config" / "rob.json"
_MODES = ("solid", "neon", "mono", "duotone", "holo")


def save_config(config: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _instance_home():
    """(home_dir, instance_id) for the CLI, or (None, id) if unresolvable. Fail-open."""
    from core.instance import resolve_instance_id
    try:
        from core.bootstrap import _resolve_cli_data_home
        home, _, _ = _resolve_cli_data_home()
        return home, resolve_instance_id()
    except Exception:
        return None, resolve_instance_id()


def _resolve_config(seed: str | None, config_path: str | None) -> Dict[str, Any]:
    if seed:
        return default_config(seed)
    if config_path:
        return load_frozen_config(config_path)
    # prefer the STORED instance avatar (if one has been generated)
    home, instance_id = _instance_home()
    if home is not None:
        from core.instance import load_pfp_meta
        meta = load_pfp_meta(home, instance_id)
        if meta:
            try:
                return load_frozen_config(meta)
            except FrozenConfigError:
                pass
    if _DEFAULT_CONFIG_PATH.is_file():
        try:
            return load_frozen_config(_DEFAULT_CONFIG_PATH)
        except FrozenConfigError:
            pass
    return default_config()  # deferrable: no stored avatar -> render from the default seed


@click.group("pfp")
def pfp():
    """Generate, view, and manage the agent's avatar (Mindprint)."""


@pfp.command("show")
@click.option("--seed", default=None, help="Render a face from this name/seed instead of the stored one.")
@click.option("--config", "config_path", default=None, help="Render from a frozen config JSON path.")
@click.option("-a", "--animate", is_flag=True, help="Animate (breath/blink) — needs a truecolor TTY.")
@click.option("--width", default=48, show_default=True, help="Face width in terminal columns.")
def show_cmd(seed, config_path, animate, width):
    """Render the agent's face live in the terminal."""
    config = _resolve_config(seed, config_path)
    terminal_render.render(config, width=width, animate=animate)


def frozen_report(meta: Dict[str, Any], png_path, *, regenerated: bool = False) -> str:
    """The post-render report: what was made, its identity, and what to do next.

    State-aware: a DRAFT points at the setup verbs (randomize/keep); a KEPT identity
    points at view/push only — it can no longer be changed."""
    from modules.pfp.store import is_locked
    tr = meta.get("traits") or {}
    v = meta.get("voice") or {}
    lines = [
        f"avatar {'re' if regenerated else ''}generated ({meta.get('rendered_by')}) → {png_path}",
        f"face:  {meta.get('seed_hex', '?')} · {tr.get('tier', '?')} · head {tr.get('head', '?')} · "
        f"eyes {tr.get('eyes', '?')} · mouth {tr.get('mouth', '?')} · antenna {tr.get('antenna', '?')} · "
        f"aura {tr.get('aura', '?')} · {tr.get('mode', '?')}",
        f"voice: pitch {v.get('pitch', '?')} · rate {v.get('rate', '?')} · timbre {v.get('timbre', '?')}"
        "   (the agent's voice signature — spoken by voice surfaces when they land)",
    ]
    if is_locked(meta):
        lines.append("state: KEPT — this identity is permanent")
        lines.append("next:  view `polyrob pfp show` (or /pfp show) · hear `polyrob pfp say` · "
                     "set it on X/Telegram/Discord `polyrob pfp push` · "
                     "webview /identity picks it up automatically")
    else:
        lines.append("state: DRAFT — setup is still open")
        lines.append("next:  hear the voice `polyrob pfp say` · "
                     "re-roll `polyrob pfp randomize [face|voice]` · "
                     "when it feels right, KEEP IT FOREVER: `polyrob pfp keep` "
                     "(or /pfp say · /pfp randomize · /pfp keep)")
    return "\n".join(lines)


def _echo_face(meta: Dict[str, Any]) -> None:
    """Show the face inline right after a setup step (truecolor TTY only, fail-open).

    Seeing what you rolled is part of the setup loop — without this, every roll
    needs a separate `pfp show`."""
    try:
        if not (terminal_render.supports_truecolor() and sys.stdout.isatty()):
            return
        click.echo(terminal_render.frame(_core_config(meta), width=40, still=True))
    except Exception:
        pass  # the report already carries the identity in text


def _kept_message(instance_id: str) -> str:
    return (f"the {instance_id!r} avatar identity is kept — setup happens once and it "
            "cannot be changed afterwards.\nview it: `polyrob pfp show` · push it: "
            "`polyrob pfp push`")


@pfp.command("generate")
@click.option("--force", is_flag=True, help="Re-render the STORED identity's pixels (identity unchanged).")
@click.option("--seed", default=None, help="Identity seed (name) for the random face (default: instance name).")
@click.option("--variant", default=None, help="Pin the shuffle variant (reproduce a specific random face).")
@click.option("--stock", is_flag=True, help="Use the committed stock identity (avatar/config/rob.json) instead of a random one.")
@click.option("--config", "config_path", default=None, help="Frozen config JSON path (overrides --seed/--variant/--stock).")
def generate_cmd(force, seed, variant, stock, config_path):
    """Start avatar setup: mint a RANDOM draft identity (face + voice).

    Setup happens ONCE: re-roll the draft with `pfp randomize`, then freeze it
    permanently with `pfp keep`. A kept identity can never be changed. --stock uses
    the committed reference identity, --config a frozen studio export, and
    --seed/--variant pin the roll; --force only re-renders the stored identity's
    pixels (e.g. after installing the browser extra)."""
    from modules.pfp import store
    from modules.pfp.renderer import PfpRenderUnavailable
    from core.instance import load_pfp_meta, pfp_path
    home, instance_id = _instance_home()
    if home is None:
        raise click.ClickException("could not resolve the data home")

    identity_args = bool(stock or seed or variant or config_path)
    existing = load_pfp_meta(home, instance_id)
    png = pfp_path(home, instance_id)

    if png.is_file() and existing is not None:
        if store.is_locked(existing):
            if identity_args:
                raise click.ClickException(_kept_message(instance_id))
            if not force:
                click.echo(_kept_message(instance_id))
                return
            # pixels-only re-render of the SAME kept identity
            config, config_path = _core_config(existing), None
        elif not identity_args and not force:
            click.echo("a DRAFT avatar already exists:\n"
                       + frozen_report(existing, png))
            return
        elif not identity_args and force:
            config, config_path = _core_config(existing), None   # re-render the draft
        else:
            config = _build_identity(stock, seed, variant) if config_path is None else None
            force = True                                          # replace the draft
    else:
        config = _build_identity(stock, seed, variant) if config_path is None else None

    try:
        meta = store.generate_pfp(home, instance_id, config=config,
                                  config_path=config_path, force=force)
    except store.PfpLockedError as e:
        raise click.ClickException(str(e))
    except PfpRenderUnavailable as e:
        raise click.ClickException(
            f"could not render the avatar: {e}\n"
            "install the browser extra for the exact engine: pip install '.[browser]' && "
            "playwright install chromium"
        )
    _echo_face(meta)
    click.echo(frozen_report(meta, png, regenerated=force))


def _build_identity(stock: bool, seed, variant) -> Dict[str, Any]:
    if stock:
        return load_frozen_config(_DEFAULT_CONFIG_PATH) if _DEFAULT_CONFIG_PATH.is_file() \
            else default_config(seed or _DEFAULT_SEED)
    config = random_config(seed or _DEFAULT_SEED)
    if variant is not None:
        config["variant"] = variant
    return config


@pfp.command("randomize")
@click.argument("what", type=click.Choice(["all", "face", "voice"]), default="all")
def randomize_cmd(what):
    """Re-roll the DRAFT: ALL (new face + voice), FACE (keep a pinned voice), or VOICE only.

    A setup step — once the identity is kept (`pfp keep`), it can never be re-rolled."""
    from modules.pfp import store
    from modules.pfp.renderer import PfpRenderUnavailable
    from core.instance import load_pfp_meta, pfp_path
    home, instance_id = _instance_home()
    if home is None:
        raise click.ClickException("could not resolve the data home")

    meta = load_pfp_meta(home, instance_id)
    if meta is not None and store.is_locked(meta):
        raise click.ClickException(_kept_message(instance_id))
    try:
        current = _core_config(load_frozen_config(meta)) if meta else default_config()
    except FrozenConfigError:
        current = default_config()

    if what == "voice":
        config = shuffle_voice(current)
    elif what == "face":
        config = shuffle_face(current)
    else:
        config = shuffle_face(current)
        config["override"].pop("voice", None)   # full re-roll: voice follows the new variant

    try:
        new_meta = store.generate_pfp(home, instance_id, config=config, force=True)
    except store.PfpLockedError as e:
        raise click.ClickException(str(e))
    except PfpRenderUnavailable as e:
        raise click.ClickException(
            f"could not render the avatar: {e}\n"
            "install the browser extra for the exact engine: pip install '.[browser]' && "
            "playwright install chromium"
        )
    _echo_face(new_meta)
    click.echo(frozen_report(new_meta, pfp_path(home, instance_id), regenerated=True))


@pfp.command("keep")
def keep_cmd():
    """Accept the draft identity — freeze it PERMANENTLY (this cannot be undone)."""
    from modules.pfp import store
    from core.instance import pfp_path
    home, instance_id = _instance_home()
    if home is None:
        raise click.ClickException("could not resolve the data home")
    try:
        meta = store.keep_pfp(home, instance_id)
    except FileNotFoundError:
        raise click.ClickException("no avatar to keep — start setup with `polyrob pfp generate`")
    _echo_face(meta)
    click.echo(f"identity kept — this is {instance_id!r}'s face + voice, permanently.")
    click.echo(frozen_report(meta, pfp_path(home, instance_id)))


@pfp.command("say")
@click.argument("text", required=False)
def say_cmd(text):
    """HEAR the agent's voice signature through the system TTS engine.

    Works on a draft, a kept identity, or (with no avatar yet) the default seed —
    hearing is read-only. No TTS engine? The webview /identity page and
    `polyrob pfp studio` speak in the browser instead."""
    from modules.pfp.mesh import Mesh
    from modules.pfp.voice import speak_voice, VoiceUnavailable, DEFAULT_TEXT
    from core.instance import load_pfp_meta

    home, instance_id = _instance_home()
    meta = load_pfp_meta(home, instance_id) if home is not None else None
    if meta and isinstance(meta.get("voice"), dict):
        voice = meta["voice"]
    else:
        voice = Mesh(_resolve_config(None, None)).voice()   # deferrable, like `show`
    click.echo(f"voice: pitch {voice.get('pitch', '?')} · rate {voice.get('rate', '?')} · "
               f"timbre {voice.get('timbre', '?')}")
    try:
        engine = speak_voice(voice, text or DEFAULT_TEXT)
    except VoiceUnavailable as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"could not speak: {e}")
    click.echo(f"spoken via {engine}")


@pfp.command("push")
@click.option("--twitter", "do_twitter", is_flag=True, help="Push to X/Twitter (needs PFP_PUSH_TWITTER=true).")
@click.option("--telegram", "do_telegram", is_flag=True, help="Print Telegram BotFather steps (needs PFP_PUSH_TELEGRAM=true).")
@click.option("--discord", "do_discord", is_flag=True, help="Set the Discord bot avatar (needs PFP_PUSH_DISCORD=true).")
def push_cmd(do_twitter, do_telegram, do_discord):
    """Push the stored avatar to the agent's surfaces (flag-gated, idempotent)."""
    from modules.pfp import push as pushmod
    from modules.pfp import store
    from agents.task.constants import _bool_env
    from core.instance import pfp_path, pfp_dir, load_pfp_meta

    home, instance_id = _instance_home()
    png = pfp_path(home, instance_id) if home is not None else None
    if png is None or not png.is_file():
        raise click.ClickException("no avatar yet — run `polyrob pfp generate` first")
    if not store.is_locked(load_pfp_meta(home, instance_id)):
        raise click.ClickException(
            "the avatar is still a DRAFT — keep it first (`polyrob pfp keep`), "
            "then push it to the surfaces")

    if not (do_twitter or do_telegram or do_discord):
        do_twitter = do_telegram = do_discord = True  # default: attempt all surfaces (still gated)

    if do_twitter:
        if not _bool_env("PFP_PUSH_TWITTER", False):
            click.echo("twitter: disabled (set PFP_PUSH_TWITTER=true to enable)")
        else:
            try:
                h = pushmod.sha256_file(png)
                meta = load_pfp_meta(home, instance_id) or {}
                if meta.get("pushed", {}).get("twitter", {}).get("hash") == h:
                    click.echo("twitter: unchanged, skipped")
                else:
                    pushmod.push_twitter(png)
                    meta.setdefault("pushed", {})["twitter"] = {"hash": h}
                    (pfp_dir(home, instance_id) / "pfp.json").write_text(
                        json.dumps(meta, indent=2), encoding="utf-8")
                    click.echo("twitter: profile image updated ✓")
            except pushmod.TwitterCredsMissing as e:
                click.echo(f"twitter: {e}")
            except Exception as e:  # fail-open: never crash, always give the manual path
                click.echo(f"twitter: could not set avatar ({e}). Set it manually in the X app; image: {png}")

    if do_discord:
        if not _bool_env("PFP_PUSH_DISCORD", False):
            click.echo("discord: disabled (set PFP_PUSH_DISCORD=true to enable)")
        else:
            try:
                h = pushmod.sha256_file(png)
                meta = load_pfp_meta(home, instance_id) or {}
                if meta.get("pushed", {}).get("discord", {}).get("hash") == h:
                    click.echo("discord: unchanged, skipped")
                else:
                    pushmod.push_discord(png)
                    meta.setdefault("pushed", {})["discord"] = {"hash": h}
                    (pfp_dir(home, instance_id) / "pfp.json").write_text(
                        json.dumps(meta, indent=2), encoding="utf-8")
                    click.echo("discord: bot avatar updated ✓")
            except pushmod.DiscordCredsMissing as e:
                click.echo(f"discord: {e}")
            except Exception as e:  # fail-open: never crash, always give the manual path
                click.echo(f"discord: could not set avatar ({e}). Set it manually in the "
                           f"Discord developer portal (Bot → Icon); image: {png}")

    if do_telegram:
        if not _bool_env("PFP_PUSH_TELEGRAM", False):
            click.echo("telegram: disabled (set PFP_PUSH_TELEGRAM=true to enable)")
        else:
            click.echo(pushmod.telegram_instructions(png))


@pfp.command("studio")
def studio_cmd():
    """Open the browser studio to tune + export an avatar ('Copy config JSON')."""
    import webbrowser
    path = _ROOT / "avatar" / "studio.html"
    if not path.is_file():
        raise click.ClickException(f"studio not found: {path}")
    webbrowser.open(path.as_uri())
    click.echo(f"opened studio → {path}")


@pfp.command("pick")
@click.option("--seed", default=_DEFAULT_SEED, help="The identity seed (name) to build from.")
@click.option("--width", default=40, show_default=True, help="Preview width in terminal columns.")
@click.option("--out", "out_path", default=None,
              help="Also export the chosen frozen config JSON to this path.")
def pick_cmd(seed, width, out_path):
    """Interactive avatar setup: shuffle face + voice, then KEEP the identity forever.

    Saving renders + persists into the instance identity home (what the webview,
    invoice cards and `pfp push` read) and PERMANENTLY locks the identity — setup
    happens once. Starts from a random roll (or the existing draft).

    Keys:  [s] shuffle face   [v] shuffle voice   [c] recolor   [enter] keep forever   [q] quit
    """
    from modules.pfp import store
    from core.instance import load_pfp_meta
    home, instance_id = _instance_home()
    existing = load_pfp_meta(home, instance_id) if home is not None else None
    if existing is not None and store.is_locked(existing):
        raise click.ClickException(_kept_message(instance_id))

    if existing is not None:
        try:
            config = _core_config(load_frozen_config(existing))  # resume the draft
        except FrozenConfigError:
            config = shuffle_face(default_config(seed))
    else:
        config = shuffle_face(default_config(seed))   # start from a random roll
    tc = terminal_render.supports_truecolor()

    def draw():
        click.echo("\x1b[2J\x1b[H", nl=False)
        if tc:
            click.echo(terminal_render.frame(config, width=width, still=True))
        click.echo(terminal_render.text_line(config))
        click.echo("\n[s] shuffle face   [v] shuffle voice   [c] recolor   "
                   "[enter] keep forever   [q] quit")

    while True:
        draw()
        ch = click.getchar()
        if ch in ("q", "\x03", "\x04"):          # q / Ctrl-C / Ctrl-D
            click.echo("cancelled.")
            return
        if ch in ("\r", "\n"):
            _freeze_pick(config, out_path)
            return
        if ch == "s":
            config = shuffle_face(config)
        elif ch == "v":
            config = shuffle_voice(config)
        elif ch == "c":
            ov = config.setdefault("override", {})
            cur = ov.get("mode")
            ov["mode"] = _MODES[(_MODES.index(cur) + 1) % len(_MODES)] if cur in _MODES else _MODES[0]


def _freeze_pick(config: Dict[str, Any], out_path) -> None:
    """Persist a picked identity: keep (lock) it in the instance home (+ optional export)."""
    from modules.pfp import store
    from modules.pfp.renderer import PfpRenderUnavailable
    from core.instance import pfp_path
    if out_path:
        save_config(config, Path(out_path))
        click.echo(f"\nconfig exported → {out_path}")
    home, instance_id = _instance_home()
    if home is None:
        # can't reach the identity home — keep the legacy repo-config save as a fallback
        save_config(config, _DEFAULT_CONFIG_PATH)
        click.echo(f"\nsaved → {_DEFAULT_CONFIG_PATH}")
        return
    try:
        meta = store.generate_pfp(home, instance_id, config=config, force=True, locked=True)
    except store.PfpLockedError as e:
        click.echo(f"\n{e}")
        return
    except PfpRenderUnavailable as e:
        save_config(config, _DEFAULT_CONFIG_PATH)
        click.echo(f"\ncould not render ({e}); config saved → {_DEFAULT_CONFIG_PATH}")
        return
    click.echo(f"\nidentity kept — this is {instance_id!r}'s face + voice, permanently.")
    click.echo(frozen_report(meta, pfp_path(home, instance_id), regenerated=True))
