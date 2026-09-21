"""h_a23.py — the ten REPL parity verbs, registered from ONE seam (043 A23).

The verbs live one-per-module (``h_steer``, ``h_goal_one``, ``h_reject``,
``h_money_verbs`` = wallet/trade/bridge/dev, ``h_mode``, ``h_prefs``,
``h_files``) so each stays beside its own handler and out of the size-ratcheted
``handlers.py`` (``tests/test_file_size_ratchet.py``: extract, never grow).

This aggregator exists so ``build_default_registry`` adds all ten with ONE
line instead of fourteen — ``handlers.py`` sits at its ratchet ceiling and has
room for exactly one more line. Each sub-module owns its ``register(reg,
Command)`` (the ``h_inbox`` / ``h_token`` pattern); this just fans out to them.
"""
from __future__ import annotations

from cli.ui.commands import (
    h_files, h_goal_one, h_mode, h_money_verbs, h_prefs, h_reject, h_rooms, h_steer,
)


def register(reg, Command) -> None:
    """Register all ten A23 parity verbs on *reg*.

    Order is stable and cosmetic (grouped ``/help`` re-sorts by section); the
    registry raises on a duplicate name/alias, so a collision fails loudly at
    build time rather than silently shadowing an existing verb.
    """
    from cli.ui.commands import h_attach
    h_attach.register(reg, Command)
    h_steer.register(reg, Command)         # /steer (new on every seat)
    h_goal_one.register(reg, Command)      # /goal
    h_reject.register(reg, Command)        # /reject
    h_money_verbs.register(reg, Command)   # /wallet /trade /bridge /dev
    h_mode.register(reg, Command)          # /mode
    h_prefs.register(reg, Command)         # /prefs
    h_files.register(reg, Command)
    # C18/E4: /groups /mute /unmute /ban /unban /paid + /cancel /new /start —
    # nine verbs core.verbs already promised on every seat.
    h_rooms.register(reg, Command)
    # E6-E10 (2026-09-21): /claim /nft /dapp /contacts /identity — the five
    # capabilities that were agent-only until the interface audit.
    from cli.ui.commands import h_owner_reach
    h_owner_reach.register(reg, Command)
