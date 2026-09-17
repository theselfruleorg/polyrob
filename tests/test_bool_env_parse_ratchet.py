"""Open-coded boolean truth sets may only SHRINK.

``os.getenv(...).lower() in ("1", "true", "yes", "on")`` is an opt-in
ALLOW-list; the repo's one flag parser (``core.env.parse_bool``/``bool_env``)
is a falsey DENY-list (only ``none/off/false/0/no/''`` disable). The two
disagree on every other spelling — ``enabled``, ``yes please``, ``TRUE ``
— which is how a flag reads ON in one module and OFF in the one next to it
(the reflection-gate bug was exactly a parser/source mismatch). Sites that
parse a QUERY or MCP parameter rather than an env flag are legitimately
allow-lists and stay; the baseline counts them. Lower it as env sites
migrate, never raise it.
"""
import pathlib
import re

_BASELINE = 4   # 2026-09-17: core/prefs._coerce (a validated pref VALUE that rejects
                # unknown spellings), tools/mcp/param_coercion (MCP param coercion),
                # tools/defi/providers/goplus (a provider's own "1"/"true"/"True" wire
                # encoding), and one docstring in modules/memory/sqlite_memory_provider
                # — none of them reads an env flag.
_SKIP = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "deployment", "docs", "scripts", "build", "dist"}
_PAT = re.compile(
    r"""in\s*[\(\{]\s*(?:"|')(?:1|true|yes|on|True)(?:"|')\s*,\s*(?:"|')(?:1|true|yes|on|True)(?:"|')""")


def test_open_coded_truth_sets_never_grow():
    repo = pathlib.Path(__file__).resolve().parents[1]
    count = 0
    offenders = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if _SKIP.intersection(rel.parts):
            continue
        try:
            hits = len(_PAT.findall(p.read_text(errors="ignore")))
        except OSError:
            continue
        if hits:
            count += hits
            offenders[str(rel)] = hits
    assert count <= _BASELINE, (
        f"open-coded boolean truth sets grew to {count} (baseline {_BASELINE}). "
        f"Parse env flags with core.env.bool_env/parse_bool/bool_from. "
        f"Offenders: {offenders}")
