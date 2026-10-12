#!/usr/bin/env python3
"""Regenerate webview/static/app/verbs.en.json from core/verbs.py (043 palette).

The command palette (``webview/static/app/palette.js``) runs in the browser,
which imports no Python at all, so the one verb table (``core/verbs.py`` — name,
group, one-line help) is handed to it as a checked-in JSON artifact. This script
emits that artifact; the browser fetches ``/static/app/verbs.en.json``.

There is no ``copy.en.json`` generator to sit beside (the console's copy crosses
into JS on ``data-*`` attributes, not a JSON island), so this is the pattern for
a whole *table* rather than a handful of strings: generate once, commit the
artifact, and pin it with a contract test so it cannot drift from the source.

Run this whenever ``core/verbs.py`` changes — the contract test
``ops/tests/webview/test_verbs_json.py`` fails until the artifact is
regenerated:

    python scripts/gen_verbs_json.py            # rewrite the artifact
    python scripts/gen_verbs_json.py --check     # exit 1 if the artifact is stale

The artifact is checked in (not built at deploy time) because deployed installs
rsync code directories and serve ``webview/static`` as-is — there is no build
step in the console (it is deliberately stack-free HTML + CSS + plain ESM).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.verbs import GROUP_ORDER, pack_verb_names, seat_verbs  # noqa: E402

OUT = ROOT / "webview" / "static" / "app" / "verbs.en.json"


def build() -> dict:
    """The JSON the palette reads. ``verbs`` keeps :data:`core.verbs.VERB_TABLE`
    order so the palette renders each group in the same order the REPL's
    grouped help does; ``group_order`` is the section order both seats share.

    ⚠️ Filtered to the verbs the CONSOLE can actually run (``Verb.seats``). A
    palette entry the console cannot execute is a promise the surface does not
    keep — the user taps it and nothing happens.

    A verb a PACK registers (``core.verbs.pack_verb_names``, e.g. ``/x``) exists
    only in a process that loaded the pack, so this checked-in artifact leaves
    it out: it would otherwise depend on which packs the generating process
    loaded. The console still RUNS such a verb when it is typed.
    """
    return {
        "_generated_by": (
            "scripts/gen_verbs_json.py from core/verbs.py — do not edit by hand"
        ),
        "group_order": list(GROUP_ORDER),
        "verbs": [
            {
                "name": verb.name,
                "group": verb.group,
                "help": verb.help,
                "aliases": list(verb.aliases),
            }
            for verb in seat_verbs("console")
            if verb.name not in pack_verb_names()
        ],
    }


def _serialize(data: dict) -> str:
    """Deterministic bytes: 2-space indent, unicode kept, one trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list) -> int:
    text = _serialize(build())
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(
                f"{OUT.relative_to(ROOT)} is stale — run "
                "`python scripts/gen_verbs_json.py`",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(build()['verbs'])} verbs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
