"""The post-install verify probe, run from the NEW tree by ``polyrob update``.

The updater that drives an update is the OLD code; a module list hardcoded in it
goes stale the moment a module moves (1.3.x listed ``utils.gif_utils``, which
became ``agents.task.gif_utils`` — every 1.3.x → ≥1.5 update failed verify and
rolled back). So the updater runs ``python -I -m cli.update.verify_probe`` in the
updated interpreter, and the list it checks is the one that ships with the code
it checks. Exit 0 = healthy; any other exit makes the update roll back.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

#: The surfaces whose dependency moved to an extra (058): a bare
#: ``import core, cli.polyrob`` passed on an install that had lost one of them.
MODULES = (
    "core", "core.avatar", "cli.polyrob", "modules.llm.llm_factory", "tools.filesystem",
    "modules.memory.task", "modules.cards.cards", "agents.task.gif_utils",
    "tools.document_parser", "core.lazy_deps",
)

#: Read by path at runtime: the invoice card's brand mark and its two faces.
ASSETS = (
    ("assets", "brand", "polyrob-mark-256.png"),
    ("assets", "fonts", "dejavu", "DejaVuSans.ttf"),
    ("assets", "fonts", "dejavu", "DejaVuSans-Bold.ttf"),
)


def problems() -> list:
    out = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — every failure is a finding
            out.append(f"import {name}: {type(exc).__name__}: {exc}")
    try:
        import modules.cards as cards
        root = pathlib.Path(cards.__file__).resolve().parents[2]
        missing = [str(root.joinpath(*a)) for a in ASSETS if not root.joinpath(*a).is_file()]
        if missing:
            out.append("runtime assets missing after install: " + ", ".join(missing))
    except Exception as exc:  # noqa: BLE001
        out.append(f"asset check: {type(exc).__name__}: {exc}")
    return out


def main() -> int:
    found = problems()
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
