"""SSOT for the set of SQLite databases POLYROB creates under a data-home.

The `polyrob update` backup/rollback flow must snapshot *every* user database, not
just the relational `bot.db` that the migration system knows about. The autonomy,
memory, and surface layers each open their own sidecar DB directly under the data
home (`memory.db`, `goals.db`, `cron.db`, `skill_usage.db`, `users.db`,
`tg_dedup.db`) with ad-hoc `CREATE TABLE IF NOT EXISTS` — invisible to
`migrations/`. This module is the single place that enumerates them, so protecting a
new sidecar DB is a one-line change here and backup/restore stay in sync.

Layout (see modules/memory/backend_factory.py, agents/task/goals/board.py,
cron/runner.py, modules/skills/skill_usage.py, surfaces/telegram/harness.py):
- `bot.db`      -> ``<data_home>/database/bot.db``  (modules/database/database_manager.py)
- everything else -> ``<data_home>/<name>``
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

# Sidecar DBs opened directly under the data-home by the autonomy/memory/surface layers.
SIDECAR_DB_NAMES = (
    "memory.db",
    "goals.db",
    "cron.db",
    "skill_usage.db",
    "users.db",
    "tg_dedup.db",
    "autonomy_state.db",
    # D11 (2026-07-11): previously missing from the manifest — backup/rollback
    # silently skipped them. R-2 T1 (2026-07-17): telemetry_events.db now resolves
    # via core.runtime_paths.sidecar_db_path — <data_home>/telemetry_events.db,
    # matching this manifest (axis parity pinned by
    # tests/unit/core/test_sidecar_db_path.py). A PRE-EXISTING install may still
    # hold the file at the legacy session-tree location (<data_root>/…) until the
    # one-shot boot relocation runs; cli/update/context.py passes those legacy
    # paths via `extra_dbs` so backups never miss them either way.
    # (TELEMETRY_EVENT_LOG_PATH still overrides; off-home overrides also ride
    # `extra_dbs`.)
    "telemetry_events.db",
    "surfaces.db",       # core/surfaces/bootstrap.py + telegram outbound allowlist
    "pairing.db",        # core/pairing.py
    "messages.db",       # agents/task/agent/messages/persistence.py (opt-in mirror)
    "wa_dedup.db",       # surfaces/whatsapp/harness.py
    "email_dedup.db",    # surfaces/email/harness.py
    "defi_tokens.db",    # core/wallet/tokens.py (frozen first-seen token metadata)
    # 037: in-flight cross-chain bridges. MUST be in a backup — an unbacked
    # in-flight row is an unrecoverable bridge (core/wallet/bridge_guard.py).
    "bridges.db",        # core/wallet/bridge_guard.py
    # 046: the assets the treasury may be paid in. An operator grant, and the
    # only record of the FROZEN decimals every amount comparison reads — losing
    # it would make every non-USDC invoice unresolvable.
    "payment_assets.db",   # core/payments/assets.py
    # 046: paid room-action offers. A PAID, not-yet-applied row is an
    # OBLIGATION — losing it loses both the effect and the credit owed.
    "room_actions.db",     # core/surfaces/room_action_store.py
    # T1 (2026-07-16): surface/deploy sidecars that were missing — backup/rollback
    # silently skipped them (second generation of the D11 class; the grep-based
    # contract test in tests/unit/core/test_db_manifest_sidecars.py now guards this).
    "slack_dedup.db",       # surfaces/slack/harness.py
    "signal_dedup.db",      # surfaces/signal/harness.py
    "discord_dedup.db",     # surfaces/discord/harness.py
    "x_dedup.db",           # surfaces/x/harness.py
    "wa_window.db",         # surfaces/whatsapp/harness.py (24h send-window tracker)
    "group_allowlist.db",   # core/surfaces/access.py (group ingress allowlist)
    "conversations.db",     # core/surfaces/bootstrap.py (ConversationStore)
    "outbox.db",            # core/surfaces/bootstrap.py (durable outbound queue)
    "surface_state.db",     # core/surfaces/bootstrap.py (surface cursor/state KV)
    "deployed_apps.db",     # tools/hf_deploy/registry.py
    "artifacts.db",         # core/artifacts.py (one row per produced file)
    "publications.db",      # core/publish.py (the ship rail: slug -> served dir)
    "app_services.db",      # core/app_service/registry.py (032: durable app rows)
    "dead_targets.db",      # core/surfaces/bootstrap.py (DeadTargetStore, T1.5)
    # 2026-08-22: THIRD generation of the D11 class. Every correspondent binding
    # — who the agent may talk to and which session their replies route into —
    # lived only here, and backup/rollback skipped the file. A rollback silently
    # unbound every correspondent. Opened from cli/commands/{owner,email,gateway}.py,
    # webview/pages.py and surfaces/telegram/harness.py; the grep contract test
    # only covers surfaces/, so it stayed invisible until a surface-tier call site
    # appeared.
    "correspondents.db",    # core/surfaces/correspondents.py (CorrespondentRegistry)
    "token_denylist.db",    # core/token_denylist.py (043 W5: revoked owner-session jtis)
    "wakes.db",             # core/wake_queue.py (043 W10: cross-process session wakes)
    "dapp_sessions.db",     # core/dapp_session_store.py (043 A37: durable dapp envelopes)
    # 043 A35: the rail-written open-position store. Losing it loses the agent's
    # cost basis for every open position, so the Money Book's profit/loss falls
    # back to "no entry recorded" — recoverable, but a backup must keep it.
    "open_positions.db",    # core/open_positions.py (PolicyGate.record writer)
    # 2026-09-18: FOURTH generation of the D11 class. Opened from agents/, a tier
    # the grep contract test did not walk. Session→owner_pid rows under
    # SESSION_REGISTRY_BACKEND=sqlite; a rollback that skips it leaves live
    # sessions routed to a PID that no longer exists.
    "session_registry.db",  # agents/task_agent_lite.py (SqliteSessionRegistry)
    # 057 WS-F: standing external-rail refusals (SMTP 535, X API 402, missing
    # key). The row carries first_seen, which is the ONLY record of when an
    # outage began — losing it re-probes every dead rail and resets every
    # "since <first failure>" line to now.
    "verdicts.db",          # core/credential_verdicts.py
)

_PathLike = Union[str, Path]


# bot.db is NOT at a single fixed path: its real location is config-driven (the
# ``DB_PATH`` env / ``AgentConfig.db_path``, default ``data/database/bot.db`` anchored to
# the data-home — R-2 B2 made DB_PATH real: ``modules/database/database_manager.py::
# resolve_bot_db_path``). Prod ships ``DB_PATH=<root>/data/database/bot.db``; the
# historical guess was ``<root>/database/bot.db``. When we can't resolve the config
# value we must treat
# ALL known layouts as candidates so a snapshot never silently skips the live DB — a
# missed bot.db means a rollback wipes the user's real data (data-loss).
_BOT_DB_RELATIVE_LAYOUTS = (
    ("database", "bot.db"),
    ("data", "bot.db"),
    ("data", "database", "bot.db"),
)


def candidate_sqlite_dbs(
    data_home: _PathLike,
    *,
    bot_db_path: Optional[_PathLike] = None,
    extra_dbs: Optional[List[_PathLike]] = None,
) -> List[Path]:
    """Every SQLite DB path POLYROB *may* create under ``data_home``.

    Returns absolute, de-duplicated paths whether or not they exist yet. ``bot_db_path``
    overrides the bot.db candidates with a single explicit path (e.g. an absolute
    config-resolved ``DB_PATH``); when given, the default layouts are not included.
    ``extra_dbs`` are always appended (e.g. a config-resolved DB that lives OUTSIDE
    ``data_home``, such as prod's ``/opt/polyrob/...``).
    """
    home = Path(data_home).resolve()
    if bot_db_path:
        bot_cands: List[Path] = [Path(bot_db_path).resolve()]
    else:
        bot_cands = [home.joinpath(*layout) for layout in _BOT_DB_RELATIVE_LAYOUTS]
    paths: List[Path] = list(bot_cands)
    paths.extend(home / name for name in SIDECAR_DB_NAMES)
    if extra_dbs:
        paths.extend(Path(p) for p in extra_dbs)

    seen: set = set()
    out: List[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def all_sqlite_dbs(
    data_home: _PathLike,
    *,
    bot_db_path: Optional[_PathLike] = None,
    extra_dbs: Optional[List[_PathLike]] = None,
) -> List[Path]:
    """The subset of :func:`candidate_sqlite_dbs` that currently exists on disk.

    This is what backup/rollback iterate over — only real files are snapshotted.
    """
    return [
        p for p in candidate_sqlite_dbs(
            data_home, bot_db_path=bot_db_path, extra_dbs=extra_dbs)
        if p.is_file()
    ]


def all_sqlite_dbs_for(paths, *, bot_db_path: Optional[_PathLike] = None) -> List[Path]:
    """Convenience wrapper accepting a ``RuntimePaths``-like object (``.data_home``)."""
    return all_sqlite_dbs(paths.data_home, bot_db_path=bot_db_path)
