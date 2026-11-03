"""098 S2/S8 ratchets: unguarded status writes, whole-document JSON writes and
non-atomic JSON state files may only SHRINK.

* ``STATUS_ALLOW`` — an ``UPDATE … SET status|state=…`` whose WHERE has no
  status/state predicate (rule S2: one guarded write per transition — use
  ``core.state.sql.cas_transition``).
* ``PAYLOAD_ALLOW`` — an ``UPDATE … SET payload|metadata=?`` (rule S8: update the
  keys you own with ``json_set`` — ``core.state.sql.json_patch``).
* ``FILE_ALLOW`` — ``json.dump`` onto an ``open(…, "w")`` under core/agents/cron/
  modules (rule S8: ``core.state.files.atomic_write_json`` / ``locked_update_json``).

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
        end = re.search(r"[\"']\s*,\s*[\(\[\w]|[\"']\s*\)", chunk)
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
    status, payload, files = {}, {}, {}
    for p in py_files(ROOTS):
        if p.startswith("core/state/"):
            continue
        src = open(p, encoding="utf-8", errors="ignore").read()
        for t, n in unguarded_status(src).items():
            status[f"{p}::{t}"] = n
        for t, n in whole_payload(src).items():
            payload[f"{p}::{t}"] = n
        if p.split("/")[0] in FILE_ROOTS:
            n = json_file_writes(src)
            if n:
                files[p] = n
    return status, payload, files


STATUS_ALLOW = {
    'agents/task/goals/board.py::goals': 12,
    'agents/task/goals/board_asks.py::goals': 1,
    'agents/task/goals/board_deps.py::goals': 2,
    'core/self_evolution.py::curated_memory': 1,
    'core/signer/store.py::approvals': 1,
    'core/signer/store.py::nonces': 1,
    'core/surfaces/outbound_queue.py::outbound_queue': 2,
    'core/surfaces/room_action_store.py::room_action_offers': 3,
    'core/wake_queue.py::session_wakes': 6,
    'core/wallet/submission_journal.py::submissions': 2,
    'cron/jobs.py::cron_jobs': 5,
    'modules/memory/sqlite_curated_store.py::curated_memory': 3,
    'modules/x402/invoicing.py::x402_payment_requests': 1,
    'modules/x402/settlement_holds.py::x402_payment_requests': 2,
    'modules/x402/subscriptions.py::subscriptions': 4,
    'modules/x402/subscriptions.py::x402_payment_requests': 1,
    'modules/x402/x402_integration.py::x402_payment_requests': 1,
}

PAYLOAD_ALLOW = {
    'core/surfaces/outbound_queue.py::outbound_queue': 1,
    'modules/database/conversation_contexts.py::conversation_contexts': 1,
}

FILE_ALLOW = {
    'agents/task/agent/core/construction.py': 1,
    'agents/task/agent/messages/persistence.py': 2,
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
    'modules/memory/task/hierarchical_memory.py': 1,
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
