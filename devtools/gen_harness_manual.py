#!/usr/bin/env python3
"""Regenerate the ``harness-manual`` skill (093 C2).

One artifact: ``data/prompts/skills/harness-manual/SKILL.md`` — "when a tool is
gated for reason X, who fixes it, with which verb, and when it applies". It is
GENERATED from the three SSOTs, so it cannot drift from them:

* ``core/tool_gates.py`` — ``REMEDY_ACTORS`` (who / when / the verb or line),
  next to ``REMEDIES`` (the remedy text the agent reads in a tool result);
* ``core/verbs.py`` — the help line of every owner verb the table names.

The skill is PINNED for owner chat seats
(``agents/task/agent/core/construction.py::owner_seat_skill_ids``). ⚠️ Never
rename the skill id ``harness-manual``: a pin names it.

    python devtools/gen_harness_manual.py            # rewrite the skill
    x    # exit 1 if stale

``tests/unit/core/test_093_remedy_actors.py`` fails until you re-run it after a
change to ``REMEDY_ACTORS`` or a named verb's help line.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "prompts" / "skills" / "harness-manual" / "SKILL.md"

FRONTMATTER = """---
name: harness-manual
description: A tool you need is missing or refuses - read the true reason, say who fixes it and when, offer the one-tap fix.
license: MIT
metadata:
  polyrob-when: 'A tool you need is missing, gated or refuses'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-version: '1'
  polyrob-generated: 'devtools/gen_harness_manual.py from core/tool_gates.py REMEDY_ACTORS + core/verbs.py; do not edit'
---"""

# The owner-seat prompt rule (data/prompts/system/harness.md) already says
# "agent_status(tool=) first, ONE message, no retry"; the pinned body carries
# only what the rule does not, so the first call stays inside its budget.
RULES = """# Harness manual

- Never name a cause you did not read from a tool result.
- Never advise a restart unless the remedy says restart.
- An operator fix is never a card: `propose_setting` files it; say "this needs
  the operator; I filed it as #N".

## Gate — who · when · fix"""


def _verbs_named(actors) -> list:
    names = []
    for a in actors:
        for m in re.findall(r"(?<![\w<])(/[a-z][a-z_-]*)", a.verb):
            if m not in names:
                names.append(m)
    return names


def render() -> str:
    from core.tool_gates import REMEDY_ACTORS, STATUS_REASONS
    from core.verbs import verb_for
    rows = []
    for key in ("load_tool",) + tuple(STATUS_REASONS):
        a = REMEDY_ACTORS[key]
        label = "loadable" if key == "load_tool" else key
        rows.append(f"- {label}: {a.who} · {a.when} · {a.verb}")
    used = [REMEDY_ACTORS[k] for k in ("load_tool",) + tuple(STATUS_REASONS)]
    # Every verb the table names must be a real owner verb (drift fails --check).
    missing = [n for n in _verbs_named(used) if verb_for(n) is None]
    if missing:
        raise SystemExit(f"REMEDY_ACTORS names unknown verbs: {missing}")
    return "\n".join([FRONTMATTER, RULES, *rows]) + "\n"


def main(argv) -> int:
    text = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale — run python devtools/gen_harness_manual.py")
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
