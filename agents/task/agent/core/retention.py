"""Retention — the one always-on sweep of the knowledge and record stores (097 D4).

Before 097 every retention window (memories 365 d / autonomous 45 d, episodes
90 d, session trees, note staling) ran inside the SKILL curator's tick, and that
ticker starts only when ``CURATOR_ENABLED`` — a member of the local autonomy
bucket that no posture turns on. A server, or a local install with autonomy off,
therefore never pruned anything, and every documented window was a promise
nobody kept.

Retention is housekeeping, not autonomous work: it is started unconditionally
by the shared autonomy runtime and is NOT pause-gated (an owner pause stops the
agent's work; it does not stop old data from expiring). Each window keeps its
own setting (``MEMORY_RETENTION_DAYS``, ``EPISODIC_RETENTION_DAYS``,
``SESSION_RETENTION_DAYS``, ``KB_WRITTEN_RETENTION_DAYS``; ``<= 0`` disables
one), and note consolidation keeps its own ``KNOWLEDGE_CURATOR_ENABLED`` gate.

Every step is fail-open: one store that refuses never stops the others.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: How often the sweep runs. Every window is measured in days, so a few hours
#: is plenty, and the first pass runs at startup.
RETENTION_INTERVAL_SEC = 6 * 3600

#: 097 D3: a document the agent wrote and nobody has read (no search returned
#: it) for this long leaves the knowledge base. Only the ``written`` collection
#: ages out — what the owner ingested by hand never does.
DEFAULT_KB_WRITTEN_RETENTION_DAYS = 90


def kb_written_retention_days() -> int:
    from core.env import int_env
    return int_env("KB_WRITTEN_RETENTION_DAYS", DEFAULT_KB_WRITTEN_RETENTION_DAYS)


class RetentionSweeper:
    """One pass over every retention window. Synchronous body; run it off-loop."""

    def __init__(self, *, clock=time.time, data_dir: Optional[str] = None):
        self._now = clock
        self.data_dir = data_dir

    async def run_once(self) -> Dict[str, Any]:
        # SQLite deletes and a directory walk: never on the event loop.
        return await asyncio.to_thread(self.run_blocking)

    def run_blocking(self) -> Dict[str, Any]:
        self._prune_episodes()
        self._prune_memories()
        self._prune_sessions()
        self._consolidate_notes()
        removed = self._age_out_written_kb()
        return {"kb_written_removed": removed}

    def _age_out_written_kb(self) -> int:
        """097 D3: drop written documents nobody has read for N days."""
        try:
            days = kb_written_retention_days()
            if days <= 0:
                return 0
            from modules.memory.registry import get_memory_registry
            from modules.memory.sqlite_kb_store import WRITTEN_COLLECTION
            prov = get_memory_registry().active()
            if prov is None or not hasattr(prov, "kb_unread_sources"):
                return 0
            stale = prov.kb_unread_sources(collection=WRITTEN_COLLECTION,
                                           before_ts=self._now() - days * 86400)
            from core.async_bridge import run_coroutine_sync
            removed = 0
            for user_id, source in stale:
                if run_coroutine_sync(prov.kb_remove(
                        user_id=user_id, collection=WRITTEN_COLLECTION, source=source),
                        timeout=30.0):
                    removed += 1
            logger.info("retention: %d written document(s) unread for %dd left the "
                        "knowledge base", removed, days)
            return removed
        except Exception:
            logger.warning("written-document age-out skipped", exc_info=True)
            return 0

    def _prune_episodes(self) -> None:
        """Episodic activity-ledger retention sweep (Task 7), on the always-on
        retention ticker (097) — never the write path. Global sweep (all
        tenants); fail-open so a memory-backend hiccup never breaks the tick."""
        try:
            from core.config_policy import AutonomyConfig
            from modules.memory.registry import get_memory_registry
            prov = get_memory_registry().active()
            if prov is not None and hasattr(prov, "prune_episodes"):
                cutoff = int(self._now()) - AutonomyConfig.episodic_retention_days() * 86400
                removed = prov.prune_episodes(older_than_ts=cutoff)
                if removed:
                    logger.info("retention: pruned %d old episodes", removed)
        except Exception:
            logger.warning("episodic prune skipped", exc_info=True)

    def _prune_sessions(self) -> None:
        """Session-TREE retention (WS-K2, 2026-09-22) — the third window, on the
        same cadence as the other two and never on the write path. ``sweep``
        records its outcome; the status section reads that record (097 W9).

        `memories` (365 d) and `episodes` (90 d) were swept here; the largest
        store on disk was swept by nobody (prod: 2,449 trees, 5.0 GB, 694 over
        30 days old). The sweep is capped per tick so a backlog drains over
        several runs instead of deleting 694 directories while the agent serves
        turns, and it keeps any tree that holds a registered artifact, is named
        by a live goal, or is still owned by a worker.

        Fail-open like its two siblings: an unreadable goals.db or artifacts.db
        raises inside `plan_sweep` and REFUSES the sweep, which is the safe
        direction — nothing is deleted on a read we could not make.
        """
        try:
            from core.runtime_paths import data_dir_or_home
            from core.session_retention import retention_days, sweep
            if retention_days() <= 0:
                return
            out = sweep(data_dir_or_home(self.data_dir))
            if out.get("removed") or out.get("failed"):
                logger.info("retention: session sweep %s", out)
        except Exception:
            logger.warning("session sweep skipped", exc_info=True)

    def _consolidate_notes(self) -> None:
        """C4 (2026-07-11): mechanical note consolidation — the notes twin of the
        skill staleness loop above. Archives never-read agent-authored notes past
        KNOWLEDGE_NOTE_STALE_DAYS and collapses exact duplicates (provider does
        the SQL; owner-authored notes are exempt there). Archive-only, per-note
        self_modification audit events, fail-open. Gated
        KNOWLEDGE_CURATOR_ENABLED (safe-local group). Deliberately LLM-free —
        the aux-model clustering/contradiction pass stays deferred until a
        concrete merge policy exists (the CURATOR_LLM_MERGE lesson)."""
        try:
            from core.config_policy import AutonomyConfig
            if not AutonomyConfig.knowledge_curator_enabled():
                return
            from modules.memory.registry import get_memory_registry
            prov = get_memory_registry().active()
            if prov is None or not hasattr(prov, "consolidate_notes"):
                return
            cutoff = int(self._now()) - AutonomyConfig.knowledge_note_stale_days() * 86400
            res = prov.consolidate_notes(stale_before_ts=cutoff)
            for reason, key in (("stale", "archived_stale"),
                                ("duplicate", "archived_dupes")):
                for user_id, note_id in res.get(key, []):
                    self._note_ev("archive", note_id, user_id, reason)
            counts = {k: len(v) for k, v in res.items() if v}
            if counts:
                logger.info("retention: knowledge consolidation %s", counts)
        except Exception:
            logger.warning("knowledge consolidation skipped", exc_info=True)

    @staticmethod
    def _note_ev(action: str, note_id, user_id: str, reason: str) -> None:
        """Note lifecycle transitions are self-modifications too. Fail-open."""
        try:
            from core.self_events import emit_self_modification
            emit_self_modification(kind="note", action=action, item_id=str(note_id),
                                   user_id=user_id or "", created_by="curator",
                                   source="curator", reason=reason)
        except Exception as e:
            logger.debug("curator note event emit skipped: %s", e)

    def _prune_memories(self) -> None:
        """Cross-session `memories` retention sweep (B3, 2026-07-11) — same
        contract as the episode sweep: curator cadence only, all tenants,
        fail-open. MEMORY_RETENTION_DAYS <= 0 disables. Only rows with a B2
        provenance stamp are age-prunable (legacy rows are exempt inside the
        provider). Rows an autonomous (cron/goal) run wrote age out after the
        shorter `retention.AUTONOMOUS_RETENTION_DAYS` (B2, 2026-10-11). The
        count is logged on EVERY pass, zero included — a sweep that never
        deletes must be visible as one."""
        try:
            from core.config_policy import AutonomyConfig
            from modules.memory.registry import get_memory_registry
            from modules.memory.retention import autonomous_window_days
            days = AutonomyConfig.memory_retention_days()
            if days <= 0:
                logger.info("retention: memory retention disabled (MEMORY_RETENTION_DAYS=%d)", days)
                return
            prov = get_memory_registry().active()
            if prov is not None and hasattr(prov, "prune_memories"):
                auto_days = autonomous_window_days(days)
                now = int(self._now())
                removed = prov.prune_memories(older_than_ts=now - days * 86400,
                                              autonomous_older_than_ts=now - auto_days * 86400)
                logger.info("retention: pruned %d old memories (window %dd, autonomous runs %dd)",
                            removed, days, auto_days)
        except Exception:
            logger.warning("memory prune skipped", exc_info=True)
        # 025: an unpromoted memory SCOPE is working memory, not an archive —
        # purged after RETENTION_DAYS. Only while scopes are ON (OFF shows every
        # row as shared, and a sweep must never delete what a reader can see).
        try:
            from modules.memory.registry import get_memory_registry
            from modules.memory.scope import scopes_enabled
            from modules.memory.sqlite_scope_store import scope_age_cutoff
            prov = get_memory_registry().active()
            if scopes_enabled() and prov is not None and hasattr(prov, "purge_stale_scopes"):
                purged = prov.purge_stale_scopes(older_than_ts=scope_age_cutoff(self._now()))
                if purged:
                    logger.info("retention: purged %d unpromoted scoped memories", purged)
        except Exception:
            logger.warning("memory scope retention skipped", exc_info=True)


__all__ = ["DEFAULT_KB_WRITTEN_RETENTION_DAYS", "RETENTION_INTERVAL_SEC",
           "RetentionSweeper", "kb_written_retention_days"]
