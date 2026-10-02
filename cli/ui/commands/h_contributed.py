"""REPL commands for owner verbs a pack contributed (067 P5a).

``core.verbs.register_verbs`` lets a pack contribute a verb row and, per seat,
a handler reference. For the ``repl`` seat the reference names a ``Command``
handler ``(ctx: CommandContext)``; this registrar adds one ``Command`` per such
verb that the built-in registry does not already carry. Today's money verbs
carry no reference (their built-in ``h_*.py`` registrations run them), so this
adds nothing until 067 P5b moves them into the wallet pack.
"""
from __future__ import annotations

import importlib


def _resolve(ref: str):
    mod, _, attr = ref.partition(":")
    return getattr(importlib.import_module(mod), attr)


def register(reg, Command) -> None:
    # A PACK registers its verbs in the loader's phase 2 (the `owner.verbs` hook,
    # e.g. the x pack's `/x`). The registry can be built before the toolset
    # resolution runs phase 2, so run it here — idempotent, once per process.
    from core.packs.loader import load_packs
    load_packs()
    from core.verbs import VERB_TABLE, handler_ref, registered_verbs
    contributed = {v.name for v in registered_verbs()}
    for verb in VERB_TABLE:
        if verb.name not in contributed or not verb.runs_on("repl"):
            continue
        name = verb.name.lstrip("/")
        ref = handler_ref("repl", verb.name)
        if ref is None or reg.lookup(name) is not None:
            continue
        reg.register(Command(name, _resolve(ref), verb.help,
                             aliases=tuple(a.lstrip("/") for a in verb.aliases),
                             group=verb.group))
