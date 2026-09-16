"""`polyrob identity` — one seat for the instance's identity surfaces (043 A14/A25).

Mounts the existing, unmodified `soul`, `persona`, and `pfp` command groups
under `identity soul` / `identity persona` / `identity avatar`. This is a
routing seam only — each mounted group keeps its own callback, options, and
subcommands. `polyrob soul`, `polyrob persona`, and `polyrob pfp` remain
directly invocable at their historic top-level names forever (043 §4.1: an
alias invokes forever, it is never removed) — they are only hidden from the
grouped `--help` listing (`cli/polyrob.py::_COMMAND_ALIASES`).
"""
from __future__ import annotations

import click


@click.group("identity")
def identity():
    """The instance's identity: SOUL, persona (voice), and avatar (Mindprint)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _mount() -> None:
    # Imported inside the factory (not at module top) so loading this module
    # does no more work than loading `soul`/`persona`/`pfp` already did on
    # their own — the lazy-loading contract in cli/polyrob.py (_LAZY_SUBCOMMANDS)
    # stays honest either way.
    from cli.commands.persona import persona
    from cli.commands.pfp import pfp
    from cli.commands.soul import soul

    identity.add_command(soul, name="soul")
    identity.add_command(persona, name="persona")
    identity.add_command(pfp, name="avatar")


_mount()
