"""P0.5 (proposal 018): a pref key can never ship DEAD again.

The 2026-07-17 config review found 4 keys that were settable, persisted and
displayed while their enforcement sites read the env flag directly — the
write-only trap. This ratchet makes that structurally impossible: every key
marked ``enforced`` in PREF_SCHEMA must have at least one literal
``resolve("<key>"...)`` / ``resolve_with_source("<key>"...)`` consumer in
production code outside ``core/prefs.py``. Display surfaces iterate the schema
with a key VARIABLE (``display_effective(key)``), so they never satisfy the
pattern — a match is a genuine enforcement-site read.

Adding a new key? Either wire a consumer (the ``effective_*`` house pattern)
or mark it ``enforcement=ENFORCEMENT_ADVISORY`` — silence is not an option.
"""
import re
from pathlib import Path

from core.prefs import ENFORCEMENT_ENFORCED, PREF_SCHEMA

_REPO = Path(__file__).resolve().parents[3]
_SKIP_PARTS = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
               "deployment", "docs", "scripts"}


def _production_sources() -> list[tuple[Path, str]]:
    out = []
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO)
        if _SKIP_PARTS.intersection(rel.parts):
            continue
        if rel == Path("core/prefs.py"):
            continue
        try:
            out.append((rel, path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    return out


def test_every_enforced_pref_key_has_a_literal_consumer():
    sources = _production_sources()
    assert sources, "repo scan came up empty — ratchet is broken"
    missing = {}
    for key, spec in PREF_SCHEMA.items():
        if spec.enforcement != ENFORCEMENT_ENFORCED:
            continue
        pat = re.compile(
            r"resolve(?:_with_source)?\(\s*[\"']" + re.escape(key) + r"[\"']")
        hits = [str(rel) for rel, text in sources if pat.search(text)]
        if not hits:
            missing[key] = "no resolve('<key>') consumer found"
    assert not missing, (
        "enforced pref keys with NO enforcement-site consumer (wire one via the "
        f"effective_* pattern, or mark the spec advisory): {missing}")
