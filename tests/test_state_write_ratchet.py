"""098 S2/S8 ratchets: unguarded status writes, whole-document JSON writes and
non-atomic JSON state files may only SHRINK.

* ``STATUS_ALLOW`` — an ``UPDATE … SET status|state=…`` whose WHERE has no
  status/state predicate (rule S2: one guarded write per transition — use
  ``core.state.sql.cas_transition``).
* ``PAYLOAD_ALLOW`` — an ``UPDATE … SET payload|metadata=?`` (rule S8: update the
  keys you own with ``json_set`` — ``core.state.sql.json_patch``).
* ``FILE_ALLOW`` — ``json.dump`` onto an ``open(…, "w")`` under core/agents/cron/
  modules (rule S8: ``core.state.files.atomic_write_json`` / ``locked_update_json``).
* ``UPSERT_ALLOW`` — an ``INSERT OR REPLACE`` whose column list holds status|state,
  or an ``ON CONFLICT … DO UPDATE SET status|state=…``: it writes the state with no
  from-state at all (a buy wrote a quarantined position back to open; a reissued id
  replaced a finished delegation).

Fixing a site? Lower (or delete) its count here. A NEW site fails: use the
primitive instead. The scan is lexical; a false positive is still listed here
with its count, never silenced in the scanner.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import os, re, collections, json
ROOTS = ["agents", "api", "cli", "core", "cron", "modules", "polyrob", "surfaces", "tools",
         "utils", "webview", "packs"]
FILE_ROOTS = ["core", "agents", "cron", "modules"]

def py_files(roots):
    for root in roots:
        for dp, dns, fs in os.walk(root):
            dns[:] = [d for d in dns if d not in ("tests", "__pycache__", "venv", "node_modules")]
            for f in fs:
                if f.endswith(".py"):
                    yield os.path.join(dp, f)

def statements(src):
    for m in re.finditer(r"UPDATE\s+(\w+)\s+SET\b", src, re.I):
        chunk = src[m.start():m.start() + 900]
        # The Python literal ends at a DOUBLE quote: a single quote is SQL text
        # (``status='paid', settled_at=?`` or ``datetime('now')``), and cutting there
        # lost the WHERE that follows.
        end = re.search(r"\"\s*,\s*[\(\[\w]|\"\s*\)", chunk)
        stmt = chunk[:end.end()] if end else chunk
        body = stmt[m.end() - m.start():]
        w = re.search(r"\bWHERE\b", body, re.I)
        yield m.group(1), (body[:w.start()] if w else body), (body[w.end():] if w else "")

STATUS = r"(?<![\w.])(status|state)"
def unguarded_status(src):
    out = collections.Counter()
    for table, sets, where in statements(src):
        if re.search(STATUS + r"\s*=", sets, re.I) and not re.search(
                STATUS + r"\s*(=|IN\b|!=|<>|NOT\s+IN|IS\b)", where, re.I):
            out[table] += 1
    return out

def upsert_status(src):
    out = collections.Counter()
    for m in re.finditer(r"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+(\w+)[\s\"']*\(([^)]*)\)",
                         src, re.I):
        if re.search(STATUS + r"\b", m.group(2), re.I):
            out[m.group(1)] += 1
    # No column list: the upsert writes EVERY column, its state ones included, and
    # the lexical column check above never saw it.
    for m in re.finditer(r"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+(\w+)\s+VALUES\b",
                         src, re.I):
        out[m.group(1)] += 1
    for m in re.finditer(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\b", src, re.I):
        chunk = src[m.end():m.end() + 1500]
        nxt = re.search(r"\b(INSERT|(?<!DO )UPDATE|DELETE|SELECT)\b", chunk, re.I)
        chunk = chunk[:nxt.start()] if nxt else chunk
        c = re.search(r"\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\s+SET\b", chunk, re.I | re.S)
        if not c:
            continue
        sets = chunk[c.end():]
        w = re.search(r"\bWHERE\b|\"\s*,\s*[\(\[\w]|\"\s*\)", sets, re.I)
        if re.search(STATUS + r"\s*=", sets[:w.start()] if w else sets, re.I):
            out[m.group(1)] += 1
    return out

def whole_payload(src):
    out = collections.Counter()
    for table, sets, _ in statements(src):
        if re.search(r"(?<![\w.(])(payload|metadata|meta|payload_json|data_json)\s*=\s*\?", sets, re.I):
            out[table] += 1
    return out

def json_file_writes(src):
    if "json.dump(" not in src:
        return 0
    if not re.search(r"open\([^)]*[\"']w[b+]?[\"']", src):
        return 0
    return len(re.findall(r"json\.dump\(", src))

def collect():
    status, payload, files, upserts = {}, {}, {}, {}
    for p in py_files(ROOTS):
        if p.startswith("core/state/"):
            continue
        src = open(p, encoding="utf-8", errors="ignore").read()
        for t, n in unguarded_status(src).items():
            status[f"{p}::{t}"] = n
        for t, n in whole_payload(src).items():
            payload[f"{p}::{t}"] = n
        for t, n in upsert_status(src).items():
            upserts[f"{p}::{t}"] = n
        if p.split("/")[0] in FILE_ROOTS:
            n = json_file_writes(src)
            if n:
                files[p] = n
    return status, payload, files, upserts


#: Each row says why it stays. Scanner false positives: the WHERE guards the state
#: through COALESCE(status, …) or an f-string predicate the lexical scan cannot see.
STATUS_ALLOW = {
    # false positive: WHERE … AND COALESCE(status, 'active') = 'pending'
    'core/self_evolution.py::curated_memory': 1,
    # read-then-write in one BEGIN IMMEDIATE: the SELECT takes only state='pending'
    'core/signer/store.py::approvals': 1,
    # false positive: f"WHERE {where}" and `where` starts with status = 'pending'
    'core/surfaces/room_action_store.py::room_action_offers': 1,
    # false positive x3: f"WHERE id=? AND {guard}", guard = status='claimed' [AND claimed_by=?]
    'core/wake_queue.py::session_wakes': 3,
    # read-then-write x2: mark_booked under the writer's exclusive journal flock (the
    # SELECT refuses booked/rejected); settle in BEGIN IMMEDIATE after a SELECT of
    # state NOT IN ('booked', 'rejected')
    'core/wallet/submission_journal.py::submissions': 2,
    # false positive: f"WHERE {where}" and `where` holds status='scheduled'
    'cron/jobs.py::cron_jobs': 1,
    # false positive x3: WHERE … COALESCE(status, 'active') IN (…) / = 'active'
    'modules/memory/sqlite_curated_store.py::curated_memory': 3,
}

PAYLOAD_ALLOW = {
    'core/surfaces/outbound_queue.py::outbound_queue': 1,
    'modules/database/conversation_contexts.py::conversation_contexts': 1,
}

#: By design: the position write runs inside its own read-then-write transaction,
#: and an allowlist re-add revives a revoked row on purpose.
UPSERT_ALLOW = {
    'core/open_positions.py::open_positions': 1,
    'core/surfaces/group_allowlist.py::group_allowlist': 1,
    'core/surfaces/outbound_allowlist.py::outbound_allowlist': 1,
    # no column list: start() runs it inside BEGIN IMMEDIATE after a SELECT that
    # refuses a live row (a new run's generation replaces a finished or dead one)
    'core/session_control.py::control': 1,
    # no column list, no state column: (bucket, ts) — a trust timestamp refresh
    'webview/login_limits.py::trusted_buckets': 1,
}

FILE_ALLOW = {
    'agents/task/agent/core/construction.py': 1,
    'agents/task/agent/messages/persistence.py': 1,
    'agents/task/agent/service.py': 1,
    'agents/task/agent/session.py': 4,
    'agents/task/agent/views.py': 1,
    'agents/task/config.py': 1,
    'agents/task/session/cleanup.py': 1,
    'agents/task/telemetry/service.py': 2,
    'agents/task/utils.py': 1,
    'core/autonomy_control.py': 1,
    'core/lazy_installer.py': 1,
    'core/session_retention.py': 1,
    'core/wallet/balance_cache.py': 1,
    'core/wallet/collection_trust.py': 1,
    'core/wallet/public_identity.py': 1,
    'modules/skills/migration.py': 1,
}


@pytest.fixture(scope="module")
def found():
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        return collect()
    finally:
        os.chdir(cwd)


def _ratchet(current, allowed, what):
    grown = {k: v for k, v in current.items() if v > allowed.get(k, 0)}
    stale = {k: (allowed[k], current.get(k, 0)) for k in allowed
             if current.get(k, 0) < allowed[k]}
    assert not grown, (f"new {what} (098): " + ", ".join(f"{k} x{v}" for k, v in grown.items()))
    assert not stale, (f"{what} fixed — shrink the allowlist: "
                       + ", ".join(f"{k} {a}->{c}" for k, (a, c) in stale.items()))


def test_unguarded_status_writes_only_shrink(found):
    _ratchet(found[0], STATUS_ALLOW, "unguarded status write")


def test_whole_payload_writes_only_shrink(found):
    _ratchet(found[1], PAYLOAD_ALLOW, "whole-payload JSON write")


def test_non_atomic_json_files_only_shrink(found):
    _ratchet(found[2], FILE_ALLOW, "non-atomic JSON state file")


def test_status_upserts_only_shrink(found):
    _ratchet(found[3], UPSERT_ALLOW, "status-writing upsert")
