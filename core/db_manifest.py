"""SSOT for every SQLite database POLYROB creates — the DB REGISTRY (W8, 2026-10-10).

One row per store (:data:`DB_REGISTRY`): its file name, where it lives (``home``),
the module that owns its schema, the schema-init callable, and whether the
``polyrob update`` backup snapshots it as a sidecar. Everything else is DERIVED:

- :data:`SIDECAR_DB_NAMES` — the backup set (rows with ``home="data"`` and
  ``backup=True``, plus every chat surface's catalog state files);
- :func:`state_db_names` — every store name the app writes (registry + surface
  catalog), the stable accessor the agent shell write guard keys on;
- :func:`db_path` — the one ``<home>/<name>`` join for a registered name.

A new store is ONE row here. ``tests/unit/core/test_db_registry.py`` fails when
shipped code names a ``*.db``/``*.sqlite``/``*.sqlite3`` file that has no row.

The `polyrob update` backup/rollback flow must snapshot *every* user database, not
just the relational `bot.db` that the migration system knows about. The sidecars
self-manage their schema (``CREATE TABLE IF NOT EXISTS``, through
``core.sqlite_util.init_schema`` where the row says so) — invisible to
`migrations/`, which owns ``bot.db`` only.

Homes:
- ``data``    -> ``<data_home>/<name>``
- ``bot``     -> config-driven ``DB_PATH`` (default ``<data_home>/database/bot.db``,
  see :data:`_BOT_DB_RELATIVE_LAYOUTS`)
- ``wallet``  -> ``<data_home>/wallet/<name>`` (backed up with the ``wallet/`` dir,
  cli/update/context.py)
- ``signer``  -> the signer's own state dir (root-owned; never in the agent backup)
- ``session`` -> one file per session directory (pruned with the session)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple, Union


@dataclass(frozen=True)
class DbSpec:
    """One SQLite store. ``schema`` names the callable (``module:qualname``)
    that creates the tables; ``""`` when the owner module creates them inline."""

    name: str
    home: str          # "data" | "bot" | "wallet" | "signer" | "session"
    owner: str         # the module that owns the schema (repo-relative path)
    schema: str = ""
    backup: bool = True
    note: str = ""


_S = DbSpec

#: Every store the app creates, except the per-surface state files the surface
#: catalog contributes (``core.surfaces.catalog.state_dbs``). Order is the
#: historical SIDECAR_DB_NAMES order — keep it; append new rows at the end.
DB_REGISTRY: Tuple[DbSpec, ...] = (
    _S("memory.db", "data", "modules/memory/sqlite_memory_provider.py",
       "SqliteMemoryProvider._init_schema"),
    _S("feedback_replays.db", "data", "modules/eip8004/feedback_replays.py",
       "consume_feedback_auth", note="signed ERC-8004 feedback grants and purchase proofs"),
    _S("login_attempts.db", "data", "webview/login_limits.py", "_trusted",
       note="owner-login budgets shared by workers"),
    _S("x_write_attempts.db", "data", "packs/x/polyrob_x/write_budget.py",
       note="shared API/browser write budget"),
    _S("goals.db", "data", "agents/task/goals/board.py", "GoalBoard._init_schema"),
    _S("cron.db", "data", "cron/jobs.py", "CronJobStore"),
    _S("skill_usage.db", "data", "modules/skills/skill_usage.py",
       "SkillUsageTracker._init_schema"),
    _S("users.db", "data", "tools/user_directory.py", "UserDirectory"),
    _S("autonomy_state.db", "data", "agents/task/agent/autonomy_state.py"),
    # D11 (2026-07-11) / R-2 T1 (2026-07-17): resolves via
    # core.runtime_paths.sidecar_db_path — a pre-existing install may still hold
    # it under the session tree until core/sidecar_relocate.py moves it;
    # cli/update/context.py passes those legacy paths via `extra_dbs`.
    _S("telemetry_events.db", "data", "core/event_log.py"),
    _S("surfaces.db", "data", "core/surfaces/outbound_allowlist.py", "OutboundAllowlist",
       note="outbound allowlist + group roles"),
    _S("pairing.db", "data", "core/pairing.py", "PairingStore._ensure_schema"),
    _S("messages.db", "data", "agents/task/agent/messages/sqlite_persistence.py",
       "SqliteMessageStore", note="opt-in message mirror"),
    _S("defi_tokens.db", "data", "core/wallet/tokens.py", "_ensure_schema",
       note="frozen first-seen token metadata"),
    # 037: an unbacked in-flight row is an unrecoverable bridge.
    _S("bridges.db", "data", "core/wallet/bridge_guard.py", "_init"),
    # 046: the only record of the FROZEN decimals every amount comparison reads.
    _S("payment_assets.db", "data", "core/payments/assets.py", "AssetStore"),
    # 046: a PAID, not-yet-applied row is an OBLIGATION.
    _S("room_actions.db", "data", "core/surfaces/room_action_store.py", "OfferStore"),
    _S("group_allowlist.db", "data", "core/surfaces/group_allowlist.py", "GroupAllowlist"),
    _S("conversations.db", "data", "core/surfaces/conversations.py", "ConversationStore"),
    _S("owner_thread.db", "data", "core/surfaces/owner_thread.py", "OwnerThreadStore",
       note="061: the ONE owner transcript"),
    _S("outbox.db", "data", "core/surfaces/outbound_queue.py", "OutboundDeliveryQueue",
       note="durable outbound queue"),
    _S("surface_state.db", "data", "core/surfaces/circuit.py", "CircuitStore",
       note="surface cursor/state KV"),
    _S("deployed_apps.db", "data", "tools/hf_deploy/registry.py", "_init_schema"),
    _S("artifacts.db", "data", "core/artifacts.py", "_init_schema",
       note="one row per produced file"),
    _S("publications.db", "data", "core/publish.py", "_init_schema",
       note="the ship rail: slug -> served dir"),
    _S("app_services.db", "data", "core/app_service/registry.py", "_init_schema",
       note="032: durable app rows"),
    _S("dead_targets.db", "data", "core/surfaces/dead_targets.py", "DeadTargetStore"),
    # 2026-08-22: a rollback that skipped it silently unbound every correspondent.
    _S("correspondents.db", "data", "core/surfaces/correspondents.py",
       "CorrespondentRegistry"),
    _S("token_denylist.db", "data", "core/token_denylist.py",
       note="043 W5: revoked owner-session jtis"),
    _S("wakes.db", "data", "core/wake_queue.py", note="043 W10: cross-process session wakes"),
    _S("dapp_sessions.db", "data", "core/dapp_session_store.py",
       note="043 A37: durable dapp envelopes"),
    _S("open_positions.db", "data", "core/open_positions.py", "_init",
       note="043 A35: the agent's cost basis for every open position"),
    _S("session_registry.db", "data", "agents/task/sqlite_session_registry.py",
       "SqliteSessionRegistry", note="session -> owner_pid under the sqlite registry"),
    _S("verdicts.db", "data", "core/credential_verdicts.py", "_db",
       note="057 WS-F: first_seen is the only record of when an outage began"),
    _S("cards.db", "data", "core/surfaces/cards.py", "CardStore",
       note="an open or confirmed card is a decision in flight"),
    _S("trading.db", "data", "core/trading_mandate.py", "init_schema",
       note="086: the owner's trading mandates; a latched breaker lives here"),
    # --- not sidecars: their own homes ------------------------------------------
    _S("bot.db", "bot", "modules/database/database_manager.py", backup=True,
       note="relational DB; schema = migrations/ + the modules/database table classes"),
    _S("token_pins.db", "wallet", "core/wallet/token_pins.py", "_connect"),
    _S("token_provenance.db", "wallet", "core/wallet/token_provenance.py"),
    _S("submissions.sqlite", "wallet", "core/wallet/submission_journal.py"),
    _S("wallet_sign.db", "wallet", "tools/wallet_sign/store.py", "WalletSignStore",
       note="090: signature replay/rate log + encrypted site credentials"),
    _S("signer.sqlite", "signer", "core/signer/store.py", "SignerStore", backup=False,
       note="the signer's own ledger; root-owned, outside the agent backup"),
    _S("control.sqlite3", "session", "core/session_control.py", "_connect", backup=False,
       note="per-session run control; pruned with the session"),
)

_BY_NAME = {s.name: s for s in DB_REGISTRY}

# Sidecar DBs opened directly under the data-home (the backup set's static part).
SIDECAR_DB_NAMES = tuple(s.name for s in DB_REGISTRY if s.home == "data" and s.backup)

# 064 F1: every chat surface's own state files (dedup stores, the WhatsApp send
# window) come from its surface-catalog row, so a new surface is backed up with
# no edit here.
from core.surfaces.catalog import state_dbs as _surface_state_dbs  # noqa: E402

SIDECAR_DB_NAMES = SIDECAR_DB_NAMES + tuple(
    n for n in _surface_state_dbs() if n not in SIDECAR_DB_NAMES)


def db_spec(name: str) -> Optional[DbSpec]:
    """The registry row for *name*, or None (a surface catalog file has no row)."""
    return _BY_NAME.get(name)


def state_db_names() -> FrozenSet[str]:
    """Every SQLite file name the app creates: the registry plus every surface
    catalog state file (pack rows included, read at call time).

    The stable accessor for the agent shell write guard's state-DB set — a store
    added to the registry is guarded with no second edit."""
    return frozenset(_BY_NAME) | frozenset(_surface_state_dbs())


def is_registered(name: str) -> bool:
    return name in state_db_names()


def db_path(name: str, data_home: Optional[Union[str, Path]] = None) -> str:
    """``<data_home>/<name>`` for a registered ``home="data"`` store (or a surface
    catalog file). *data_home* defaults to the resolved data home.

    Raises ``KeyError`` for an unregistered name — register the store first —
    and ``ValueError`` for a store that does not live directly in the data home.
    """
    spec = _BY_NAME.get(name)
    if spec is None and name not in _surface_state_dbs():
        raise KeyError(f"{name} is not in core.db_manifest.DB_REGISTRY")
    if spec is not None and spec.home != "data":
        raise ValueError(f"{name} lives in the {spec.home!r} home, not <data_home>/")
    if data_home is None or str(data_home) == "":
        from core.runtime_paths import resolve_data_home
        data_home = resolve_data_home()
    return os.path.join(str(data_home), name)

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
    # 067 P3b: a pack surface's state files join the catalog in the loader's
    # phase 1, which may run after this module's import-time snapshot.
    names = list(SIDECAR_DB_NAMES) + [n for n in _surface_state_dbs()
                                      if n not in SIDECAR_DB_NAMES]
    paths.extend(home / name for name in names)
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
