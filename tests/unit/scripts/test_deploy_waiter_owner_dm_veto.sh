#!/usr/bin/env bash
# The idle waiter vetoes a deploy while an owner DM is recent (intel deep review
# 2026-09-19): the turn marker failed twice today; the telemetry row cannot be hidden.
set -euo pipefail
here="$(cd "$(dirname "$0")/../../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/locks"
sqlite3 "$tmp/cron.db" "CREATE TABLE cron_jobs (id TEXT, status TEXT);"
sqlite3 "$tmp/goals.db" "CREATE TABLE goals (id TEXT, status TEXT);"
sqlite3 "$tmp/telemetry_events.db" "CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, attrs TEXT);"
run() { POLYROB_DATA_DIR="$tmp" DEPLOY_WHEN_IDLE_DRY_RUN=1 DEPLOY_WHEN_IDLE_POLL=1 bash "$here/scripts/deploy_when_idle.sh" "$1" 2>&1; }

# 1) no DM at all → deploys
out=$(run 2); echo "$out" | grep -q "DRY RUN: would deploy" || { echo "FAIL: idle box should deploy: $out"; exit 1; }

# 2) owner DM 30 s ago → vetoed until MAX_WAIT, exit 3, names the reason
sqlite3 "$tmp/telemetry_events.db" "INSERT INTO telemetry_events(ts,kind,attrs) VALUES (strftime('%s','now')-30,'inbound_routed','{\"surface\":\"telegram\",\"tier\":\"owner\",\"decision\":\"steer\"}');"
set +e; out=$(run 2); rc=$?; set -e
[ "$rc" = "3" ] || { echo "FAIL: recent owner DM must abort (rc=$rc): $out"; exit 1; }
echo "$out" | grep -q "owner_dm_recent=1" || { echo "FAIL: abort must name the owner DM: $out"; exit 1; }

# 3) a stranger's DM (tier=denied) does not veto
sqlite3 "$tmp/telemetry_events.db" "DELETE FROM telemetry_events; INSERT INTO telemetry_events(ts,kind,attrs) VALUES (strftime('%s','now')-30,'inbound_routed','{\"surface\":\"telegram\",\"tier\":\"denied\"}');"
out=$(run 2); echo "$out" | grep -q "DRY RUN" || { echo "FAIL: a stranger DM must not veto: $out"; exit 1; }

# 4) an owner DM older than the window does not veto
sqlite3 "$tmp/telemetry_events.db" "DELETE FROM telemetry_events; INSERT INTO telemetry_events(ts,kind,attrs) VALUES (strftime('%s','now')-3600,'inbound_routed','{\"tier\":\"owner\"}');"
out=$(run 2); echo "$out" | grep -q "DRY RUN" || { echo "FAIL: an hour-old DM must not veto: $out"; exit 1; }

# 5) unreadable telemetry DB → fail closed (not idle)
rm -f "$tmp/telemetry_events.db"
set +e; out=$(run 2); rc=$?; set -e
[ "$rc" = "3" ] || { echo "FAIL: missing telemetry must fail closed (rc=$rc): $out"; exit 1; }
echo "OK: owner-DM veto"
