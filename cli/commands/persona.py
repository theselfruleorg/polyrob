"""`polyrob persona` — author and inspect the instance's CHARACTER (F4/F0).

`polyrob soul init` scaffolds the SOUL — the operator-frozen identity docs — and
opens $EDITOR. Nothing did the same for the layer directly above it, so giving an
instance a voice meant inferring, unaided: the `<slug>.character.json` naming
convention, the four-tier directory precedence, the field set, and which of those
fields the persona renderer actually consumes.

This group mirrors `soul init` exactly. It writes into the ACTIVE config home
(`polyrob_home()`, which profile activation repoints), so a character and the
`PERSONALITY_DEFAULT_CHARACTER` that selects it move together with the profile.

The three layers, highest authority first: SOUL > character > persona override
(`POLYROB_PERSONA` / the `session.persona` pref). See docs/guide/instances.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

_SCAFFOLD_NAME_HINT = "One short line the model reads as 'who am I'."


def _rendered_note() -> str:
    from agents.personality.persona_render import (
        RENDERED_FIELDS, RENDERED_STYLE_BUCKETS, STORED_ONLY_FIELDS,
    )
    rendered = ", ".join(f for f in RENDERED_FIELDS if f != "style")
    buckets = "/".join(f"style.{b}" for b in RENDERED_STYLE_BUCKETS)
    return (f"Rendered into the persona block: {rendered}, {buckets}. "
            f"Stored but NOT rendered: {', '.join(STORED_ONLY_FIELDS)} — see "
            "docs/guide/instances.md.")


def _characters_dir() -> Path:
    """The active config home's characters/ — tier 2 of the search order.

    Resolved at CALL time, never bound at import: profile activation repoints
    ``POLYROB_HOME`` in the click group callback (see tests/test_home_binding_ratchet.py).
    """
    from core.paths import polyrob_home
    return polyrob_home() / "characters"


def _scaffold(slug: str) -> dict:
    return {
        "_rendered_fields": _rendered_note(),
        "name": slug.capitalize(),
        "modelProvider": "anthropic",
        "clients": [],
        "settings": {},
        "bio": f"I am {slug.capitalize()}. {_SCAFFOLD_NAME_HINT}",
        "lore": [
            "Replace these with a few facts that shape how I think.",
        ],
        "knowledge": [],
        "messageExamples": [],
        "postExamples": [],
        "topics": [
            "Replace with what I focus on",
        ],
        "adjectives": [
            "direct",
            "careful",
        ],
        "style": {
            "all": [
                "Replace with how I write in every context.",
            ],
            "chat": [
                "Replace with how I write in conversation.",
            ],
        },
    }


def _from_preset(preset: str, slug: str) -> dict:
    from agents.personality.persona_resolver import find_character_file

    src = find_character_file(preset)
    if src is None:
        raise click.ClickException(
            f"no character named '{preset}' in any characters dir — "
            "run `polyrob persona list` to see what is available")
    data = json.loads(src.read_text(encoding="utf-8"))
    data["_rendered_fields"] = _rendered_note()
    data["name"] = slug.capitalize()
    # Reorder so the header reads first in the file the operator is about to edit.
    return {"_rendered_fields": data.pop("_rendered_fields"), **data}


def scaffold_character(slug: str, *, preset: str | None = None,
                       characters_dir: Path | None = None,
                       force: bool = False) -> Path:
    """Write ``<characters_dir>/<slug>.character.json`` and return its path.

    The ONE scaffolder — ``polyrob persona init`` and the ``polyrob init``
    wizard's character step both call it, so the two seats can never produce
    different files. Raises ``click.ClickException`` on a bad slug, an unknown
    preset, or an existing file without *force*.
    """
    from agents.personality.persona_resolver import is_safe_character_slug

    slug = (slug or "").strip()
    if not is_safe_character_slug(slug):
        raise click.ClickException(
            f"'{slug}' is not a valid character slug — a slug is ONE filename "
            "component: letters, digits, '.', '-' and '_' only")

    chars_dir = characters_dir if characters_dir is not None else _characters_dir()
    target = chars_dir / f"{slug}.character.json"
    if target.exists() and not force:
        raise click.ClickException(
            f"{target} already exists — edit it directly or re-run with --force")

    data = _from_preset(preset, slug) if preset else _scaffold(slug)
    chars_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


def _echo_block(data: dict) -> None:
    """Print exactly what the model receives, plus what it will NOT receive."""
    from agents.personality.persona_render import (
        ignored_populated_fields, render_persona_block,
    )
    block = render_persona_block(data)
    click.echo("\n--- rendered persona block (what the model receives) ---")
    click.echo(block or "(empty — the character has no renderable fields)")
    click.echo("--- end ---")
    dropped = ignored_populated_fields(data)
    if dropped:
        click.echo(f"\nNOT rendered (stored only): {', '.join(dropped)}. "
                   "Move that voice into bio/lore/style to reach the model.")


@click.group("persona")
def persona():
    """Author and inspect this instance's character (the voice layer)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@persona.command("init")
@click.argument("slug")
@click.option("--from", "preset", default=None, metavar="NAME",
              help="Copy an existing character (e.g. writer) instead of a blank scaffold.")
@click.option("--force", is_flag=True, help="Overwrite an existing character file.")
@click.option("--no-edit", is_flag=True, help="Skip opening $EDITOR after scaffolding.")
def persona_init_cmd(slug, preset, force, no_edit):
    """Scaffold <config_home>/characters/SLUG.character.json and select it."""
    target = scaffold_character(slug, preset=preset, force=force)
    data = json.loads(target.read_text(encoding="utf-8"))
    click.echo(f"Character scaffolded → {target}")

    from core.env_file import upsert_env_var
    from core.paths import polyrob_home
    env_path = polyrob_home() / ".env"
    try:
        upsert_env_var(env_path, "PERSONALITY_DEFAULT_CHARACTER", slug, secure=True)
        click.echo(f"Selected it: PERSONALITY_DEFAULT_CHARACTER={slug} in {env_path}")
    except OSError as exc:
        click.echo(f"Warning: could not write {env_path}: {exc}", err=True)

    if not no_edit:
        click.edit(filename=str(target))
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(
                f"{target} is no longer valid JSON after editing: {exc}")

    _echo_block(data)
    click.echo("\nApplies to the NEXT session (the <identity> block is built once, "
               "at agent creation).")
    click.echo("Next: `polyrob soul init` for the frozen identity docs above this "
               "layer, or `polyrob doctor` to confirm what is live.")


@persona.command("list")
def persona_list_cmd():
    """List the templates and the characters, marking the active one."""
    from cli.persona import build_persona_listing, cli_gate_on
    from core.runtime_paths import resolve_data_home

    listing = build_persona_listing("local", Path(resolve_data_home()),
                                    gate=cli_gate_on())
    for title, rows in (("templates", listing["templates"]),
                        ("characters", listing["characters"])):
        click.echo(f"\n{title}:")
        if not rows:
            click.echo("  (none)")
        for label, detail in rows:
            click.echo(f"  {label:<28} {detail}")
    click.echo("")
    for line in listing["guidance"]:
        click.echo(line)


@persona.command("show")
@click.argument("slug", required=False)
def persona_show_cmd(slug):
    """Print the rendered persona block for SLUG (default: the ACTIVE persona)."""
    from agents.personality.persona_resolver import load_character_dict
    from cli.persona import active_persona_line, cli_gate_on, describe_active_persona
    from core.runtime_paths import resolve_data_home

    gate = cli_gate_on()

    if slug:
        data = load_character_dict(slug)
        if data is None:
            raise click.ClickException(
                f"no character named '{slug}' in any characters dir — "
                "run `polyrob persona list` to see what is available")
        from agents.personality.persona_resolver import find_character_file
        click.echo(f"character '{slug}' ({find_character_file(slug)})")
        _echo_block(data)
        return

    home = Path(resolve_data_home())
    click.echo(active_persona_line("local", home, gate=gate))
    info = describe_active_persona("local", home, gate=gate)
    if not info["gate"]:
        return
    if info["kind"] == "character":
        data = load_character_dict(info["name"])
        if data is not None:
            _echo_block(data)
            return
    from cli.persona import resolve_cli_persona
    text = resolve_cli_persona(user_id="local", home_dir=home)
    click.echo("\n--- rendered persona block (what the model receives) ---")
    click.echo(text or "(empty)")
    click.echo("--- end ---")
