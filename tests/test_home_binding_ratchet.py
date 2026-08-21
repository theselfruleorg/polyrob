"""Module-level home-binding ratchet (multi-instance W2, 2026-08-19).

Forbids MODULE-LEVEL bindings of ``polyrob_home()`` / ``resolve_data_home()``.

Why (the Hermes "Seam #1" landmine, docs/design/profile-builder.md in their
tree): a module-global like ``SKILLS_DIR = HERMES_HOME / "skills"`` binds the
home AT IMPORT TIME, so a profile selected afterwards (``-P`` sets
``POLYROB_HOME``/``POLYROB_DATA_DIR``) does not retroactively rebind it — the
process silently reads/writes the WRONG profile. The fix is always a lazy
call-time resolution (a function/property), never an import-time constant.

POLYROB had exactly one such binding (``cli/ui/app.py::DEFAULT_HISTORY_PATH``),
fixed in W2. Baseline is zero; this ratchet keeps it there.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# An UNINDENTED assignment whose right side calls one of the home resolvers.
_BINDING_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\s*(?::[^=\n]+)?=\s*[^\n=]*"
    r"(?:polyrob_home|resolve_data_home)\(\)",
    re.MULTILINE,
)

_SCAN_DIRS = ("core", "cli", "agents", "modules", "tools", "api", "webview",
              "surfaces", "cron", "utils")


def test_no_module_level_home_bindings():
    violations = []
    for d in _SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in _BINDING_RE.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                violations.append(f"{py.relative_to(REPO)}:{line_no}: {m.group(0)!r}")
    assert not violations, (
        "Module-level home binding(s) found — these freeze the home at import "
        "time and break profile selection (-P). Resolve lazily inside the "
        "function instead:\n" + "\n".join(violations)
    )
