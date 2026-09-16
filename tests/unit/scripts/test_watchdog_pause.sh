#!/usr/bin/env bash
# 031 T14: the maint/intel watchdog neither nudges nor relaunches while paused.
set -euo pipefail
tmp="$(mktemp -d)"
echo '{"paused": true, "scopes": ["oversight"]}' > "$tmp/AUTONOMY_PAUSE.json"
POLYROB_DATA_DIR="$tmp" STATE_DIR="$tmp/state" SESSION=polyrob-test-nope-session \
  bash scripts/vps_maint_watchdog.sh >/dev/null 2>&1 || true
grep -q "paused by owner" "$tmp/state/watchdog.log"
! grep -q "RELAUNCH" "$tmp/state/watchdog.log"
# a trading-only pause does not bind the watchdog (it proceeds to the session check)
echo '{"paused": true, "scopes": ["trading"]}' > "$tmp/AUTONOMY_PAUSE.json"
rm -f "$tmp/state/watchdog.log"
POLYROB_DATA_DIR="$tmp" STATE_DIR="$tmp/state" SESSION=polyrob-test-nope-session START_SCRIPT="$tmp/missing.sh" \
  bash scripts/vps_maint_watchdog.sh >/dev/null 2>&1 || true
! grep -q "paused by owner" "$tmp/state/watchdog.log" 2>/dev/null
rm -rf "$tmp"
echo OK
