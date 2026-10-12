"""Telemetry event-kind reverse contract (S9, backlog B27, 2026-08-29).

``core/event_kinds.py`` is the ONE catalog of durable telemetry event kinds
(``telemetry_events.db``). Two directions are pinned:

1. **Producers ⊆ catalog** — every string literal passed as the kind to an event-log
   ``record(...)`` call, to the x402 money emitter ``_emit(...)`` or to the owner-
   delivery ``_record(...)`` wrapper must be a catalogued kind. A new
   ``get_event_log().record("my_kind", ...)`` fails here until ``core/event_kinds.py``
   gains the constant — the status snapshot / recap / digest can only report kinds
   they know about, so a free-typed kind is invisible to every owner surface.
2. **Catalog ⊆ code** — every catalogued kind is referenced somewhere in shipped
   source (as its literal or its constant name): a constant nobody produces or reads
   is dead and must be deleted, not kept "for later".

Scanner scope: git-tracked source dirs only (never tests/scripts/docs).
"""
import ast
import re
from pathlib import Path

from tests.support.repo_index import repo_index

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("core", "polyrob", "modules", "agents", "tools", "api", "cli", "surfaces", "webview", "cron")
CATALOG = REPO_ROOT / "core" / "event_kinds.py"

# (callee attr/name, restricted-to-path-prefix or None)
PRODUCER_CALLS = (
    ("record", None),                         # TelemetryEventLog.record / get_event_log().record
    ("_emit", "modules/x402/"),               # the money-telemetry emitter
    ("_record", "core/surfaces/user_delivery.py"),
)

# A ``.record(...)`` that is NOT the event log: (path, first-arg literal). The 061 owner
# thread store's ``record(direction, ...)`` takes 'in'/'out', never an event kind.
NOT_EVENT_LOG = {
    ("core/surfaces/owner_thread.py", "in"),
    ("core/surfaces/owner_thread.py", "out"),
}


def _catalog():
    kinds = {}
    for line in CATALOG.read_text().splitlines():
        m = re.match(r'^([A-Z_]+) = "([a-z_]+)"', line)
        if m:
            kinds[m.group(1)] = m.group(2)
    assert kinds, "core/event_kinds.py has no NAME = \"kind\" rows?"
    return kinds


def _source_files():
    files = [REPO_ROOT / p for p in repo_index().tracked()
             if p.endswith(".py") and p.split("/", 1)[0] in SOURCE_DIRS]
    return [f for f in files if f.is_file() and "tests" not in f.parts and f != CATALOG]


def _callee_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _callee_name(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def _not_utf8(f):
    try:
        repo_index().text(f)
        return False
    except UnicodeDecodeError:
        return True


def _event_emit_calls(tree):
    """Resolve the shared telemetry emitter's imports; UI/socket emit is unrelated."""
    callees = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.event_log":
            callees.update(a.asname or a.name for a in node.names if a.name == "emit")
        elif isinstance(node, ast.ImportFrom) and node.module == "core":
            callees.update(f"{a.asname or a.name}.emit" for a in node.names
                           if a.name == "event_log")
        elif isinstance(node, ast.Import):
            callees.update(f"{a.asname or a.name}.emit" for a in node.names
                           if a.name == "core.event_log")
    return {node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and _callee_name(node.func) in callees}


def _producer_literals():
    found = {}
    idx = repo_index()
    for f in _source_files():
        rel = f.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(idx.text(f, errors="replace")) if _not_utf8(f) else idx.tree(f)
        except SyntaxError:
            continue
        emit_calls = _event_emit_calls(tree)
        for node in (ast.walk(tree) if _not_utf8(f) else idx.nodes(f)):
            if not (isinstance(node, ast.Call) and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            if node in emit_calls:
                found.setdefault(node.args[0].value, set()).add(rel)
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
            for callee, prefix in PRODUCER_CALLS:
                if name == callee and (prefix is None or rel.startswith(prefix)):
                    if (rel, node.args[0].value) in NOT_EVENT_LOG:
                        continue
                    found.setdefault(node.args[0].value, set()).add(rel)
    return found


def test_every_produced_kind_is_catalogued():
    kinds = set(_catalog().values())
    unknown = {k: sorted(v) for k, v in _producer_literals().items() if k not in kinds}
    assert not unknown, (
        "Event kind(s) recorded that core/event_kinds.py does not know — add the "
        f"constant so the status/recap/digest surfaces can see them: {unknown}"
    )


def test_every_catalogued_kind_is_used_in_code():
    kinds = _catalog()
    idx = repo_index()
    corpus = "\n".join(idx.text(f, errors="replace") for f in _source_files())
    # One pass over the corpus instead of two regex searches per kind: a NAME made
    # of word characters matches r"\bNAME\b" exactly when it is a whole \w+ run,
    # and a [a-z_]+ literal matches r"['\"]lit['\"]" exactly when it is a whole
    # [a-z_]+ run with a quote on each side.
    words = set(re.findall(r"\w+", corpus))
    quoted = set(re.findall(r"(?<=['\"])[a-z_]+(?=['\"])", corpus))
    dead = sorted(
        name for name, literal in kinds.items()
        if name not in words and literal not in quoted
    )
    assert not dead, (
        "Catalogued event kind(s) with no producer or consumer in shipped source — "
        f"delete the constant instead of keeping it for later: {dead}"
    )


def test_shared_emit_aliases_are_checked_without_ui_socket_emit():
    tree = ast.parse("""
from core.event_log import emit as audit
from core import event_log as telemetry
import core.event_log as el
import core.event_log

audit("one", source="test")
telemetry.emit("two", source="test")
el.emit("three", source="test")
core.event_log.emit("four", source="test")
socket.emit("not_telemetry")
ctx.emit("not_telemetry_either")
""")
    assert {n.args[0].value for n in _event_emit_calls(tree)} == {
        "one", "two", "three", "four"}
