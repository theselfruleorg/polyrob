"""Event-log ``kind`` strings were free-typed at both producer and consumer — a
rename on either side silently dropped events from the activity feed / spend
rollup / digest (audit T9, 2026-07-16). core/event_kinds.py is the SSOT; this
test greps every producer call shape into agreement.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# The three producer call shapes that stamp a kind onto the durable event log:
# TelemetryEventLog.record("<kind>", ...), the x402 _emit("<kind>", ...) wrappers,
# and the controller's _emit_governance_event("<kind>", ...). \s* spans newlines.
_PRODUCER = re.compile(
    r"(?:\.record|_emit|_emit_governance_event)\(\s*[\"']([a-z0-9_]+)[\"']")


#: A module that defines its OWN ``record(...)`` method is not a telemetry
#: producer through that name — the call shape belongs to its own store. Without
#: this guard the scan read `core/surfaces/owner_thread.py`'s
#: ``store.record("out", ...)`` (a conversation direction, not an event kind) as
#: two unregistered kinds and failed on main, 2026-09-22. Narrow on purpose: the
#: guard drops only the ``.record`` shape for such a file; `_emit` and
#: `_emit_governance_event` in the same file are still checked, and every real
#: telemetry producer calls a record it does not define.
_DEFINES_OWN_RECORD = re.compile(r"^\s*def record\(", re.M)
_RECORD_CALL = re.compile(r"\.record\(\s*[\"']([a-z0-9_]+)[\"']")


def _tracked_py():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return [REPO / line for line in out.splitlines()
            if line and not line.startswith(("tests/", "dist/", "datagen/"))]


def test_every_produced_kind_is_known():
    from core.event_kinds import KNOWN_KINDS
    unknown = {}
    for py in _tracked_py():
        if not py.exists():  # tracked but deleted in the working tree
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        found = _PRODUCER.findall(src)
        if _DEFINES_OWN_RECORD.search(src):
            own = set(_RECORD_CALL.findall(src))
            found = [k for k in found if k not in own]
        for kind in found:
            if kind not in KNOWN_KINDS:
                unknown.setdefault(kind, str(py.relative_to(REPO)))
    assert unknown == {}, (
        f"producer kinds missing from core/event_kinds.py KNOWN_KINDS: {unknown}")


def test_kinds_are_unique_and_well_formed():
    import core.event_kinds as ek
    names = [k for k in vars(ek) if k.isupper()]
    values = [getattr(ek, k) for k in names if isinstance(getattr(ek, k), str)]
    assert len(values) == len(set(values)), "duplicate kind values"
    for v in values:
        assert re.fullmatch(r"[a-z0-9_]+", v), v
