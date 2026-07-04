"""`polyrob pfp` — the agent avatar (Mindprint) command group.

Renders the agent's face LIVE in the terminal from the engine field port (no PNG).
Avatar creation is optional/deferrable: `pfp show` always works, even with no stored
avatar (it renders from a seed). `pfp pick` shuffles FACE and VOICE independently and
freezes the chosen identity into avatar/config/rob.json.
"""
from __future__ import annotations

import copy
import json
import random
import string
from pathlib import Path
from typing import Any, Dict

import click

from modules.pfp.config import load_frozen_config, FrozenConfigError
from cli.ui import terminal_render

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _ROOT / "avatar" / "config" / "rob.json"
_DEFAULT_SEED = "Rob Ottmachin"
_GENERATOR = "mindprint@v2"
_MODES = ("solid", "neon", "mono", "duotone", "holo")


def _b36_variant() -> str:
    """A shuffle variant string, matching the studio's ``"#"+base36(random)`` format."""
    n = random.randint(1, 10 ** 9)
    digits = string.digits + string.ascii_lowercase
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = digits[r] + s
    return "#" + s


def default_config(seed: str = _DEFAULT_SEED) -> Dict[str, Any]:
    return {"generator": _GENERATOR, "seed": seed, "variant": "", "size": 768, "override": {}}


def shuffle_face(config: Dict[str, Any]) -> Dict[str, Any]:
    """New look (fresh variant), PRESERVING a pinned voice (§ independent shuffle)."""
    out = copy.deepcopy(config)
    out["variant"] = _b36_variant()
    voice = (config.get("override") or {}).get("voice")
    out["override"] = {"voice": voice} if voice else {}
    return out


def shuffle_voice(config: Dict[str, Any]) -> Dict[str, Any]:
    """New voice signature ONLY; the face (seed+variant+look) is untouched."""
    out = copy.deepcopy(config)
    out.setdefault("override", {})
    out["override"]["voice"] = {
        "pitch": round(0.75 + random.random() * 0.70, 2),   # 0.75–1.45
        "rate": round(0.90 + random.random() * 0.35, 2),    # 0.90–1.25
        "timbre": round(random.random(), 2),                # 0–1
    }
    return out


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


@pfp.command("generate")
@click.option("--force", is_flag=True, help="Re-render even if the avatar already exists.")
@click.option("--config", "config_path", default=None, help="Frozen config JSON (default: avatar/config/rob.json).")
def generate_cmd(force, config_path):
    """Render + freeze the avatar into the instance identity home (idempotent)."""
    from modules.pfp import store
    from modules.pfp.renderer import PfpRenderUnavailable
    home, instance_id = _instance_home()
    if home is None:
        raise click.ClickException("could not resolve the data home")
    try:
        meta = store.generate_pfp(home, instance_id, config_path=config_path, force=force)
    except PfpRenderUnavailable as e:
        raise click.ClickException(
            f"cannot render and no reference PNG available: {e}\n"
            "install the browser extra: pip install '.[browser]' && playwright install chromium"
        )
    from core.instance import pfp_path
    click.echo(f"avatar {'re' if force else ''}generated ({meta['rendered_by']}) → {pfp_path(home, instance_id)}")


@pfp.command("push")
@click.option("--twitter", "do_twitter", is_flag=True, help="Push to X/Twitter (needs PFP_PUSH_TWITTER=true).")
@click.option("--telegram", "do_telegram", is_flag=True, help="Print Telegram BotFather steps (needs PFP_PUSH_TELEGRAM=true).")
def push_cmd(do_twitter, do_telegram):
    """Push the stored avatar to the agent's surfaces (flag-gated, idempotent)."""
    from modules.pfp import push as pushmod
    from agents.task.constants import _bool_env
    from core.instance import pfp_path, pfp_dir, load_pfp_meta

    home, instance_id = _instance_home()
    png = pfp_path(home, instance_id) if home is not None else None
    if png is None or not png.is_file():
        raise click.ClickException("no avatar yet — run `polyrob pfp generate` first")

    if not (do_twitter or do_telegram):
        do_twitter = do_telegram = True  # default: attempt both surfaces (still gated)

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
def pick_cmd(seed, width):
    """Shuffle a face + voice interactively, then freeze avatar/config/rob.json.

    Keys:  [s] shuffle face   [v] shuffle voice   [c] recolor   [enter] save   [q] quit
    """
    config = default_config(seed)
    tc = terminal_render.supports_truecolor()

    def draw():
        click.echo("\x1b[2J\x1b[H", nl=False)
        if tc:
            click.echo(terminal_render.frame(config, width=width, still=True))
        click.echo(terminal_render.text_line(config))
        click.echo("\n[s] shuffle face   [v] shuffle voice   [c] recolor   [enter] save   [q] quit")

    while True:
        draw()
        ch = click.getchar()
        if ch in ("q", "\x03", "\x04"):          # q / Ctrl-C / Ctrl-D
            click.echo("cancelled.")
            return
        if ch in ("\r", "\n"):
            save_config(config, _DEFAULT_CONFIG_PATH)
            click.echo(f"\nsaved → {_DEFAULT_CONFIG_PATH}")
            return
        if ch == "s":
            config = shuffle_face(config)
        elif ch == "v":
            config = shuffle_voice(config)
        elif ch == "c":
            ov = config.setdefault("override", {})
            cur = ov.get("mode")
            ov["mode"] = _MODES[(_MODES.index(cur) + 1) % len(_MODES)] if cur in _MODES else _MODES[0]
