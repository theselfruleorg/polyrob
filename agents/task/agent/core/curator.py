"""Skill curator (W5, Reference-parity curator.py).

Periodic maintenance of self-authored skills so the library doesn't rot: unused
authored skills go stale, then archive (recoverable, never hard-deleted); reuse
reactivates them. A skill the agent created but never reloads is noise — the curator
is what keeps "writable skills" from becoming "write-only skills".

Phase 1 (no LLM, always-on when CURATOR_ENABLED): ``apply_automatic_transitions``
over ``created_by ∈ {agent, background_review}`` skills using the W2-D load_count:
unused + age > ``CURATOR_STALE_DAYS`` → stale; stale + age > ``CURATOR_ARCHIVE_DAYS``
→ archive (via SkillManager.delete_skill → ``.archived/``); reuse reactivates.
System/pinned skills are never touched (only agent-authored ones).

(A Phase-2 aux-LLM "merge near-duplicate skills into umbrellas" step was scaffolded
but never had a real merge policy — it was a logged no-op. It was removed (2026-06-29
dead-loop prune); re-add it under its own flag when a concrete merge policy exists.)

Wired as a lifespan ticker (interval ``CURATOR_INTERVAL_HOURS``). Fail-open; a
dry-run mode lets the first rollout observe transitions without applying them.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_AUTHORED = ["agent", "background_review"]
_DAY_S = 86400.0


class SkillCurator:
    def __init__(self, skill_manager: Any, usage_store: Any, *,
                 clock=time.time, dry_run: bool = False):
        self.sm = skill_manager
        self.usage = usage_store
        self._now = clock
        self.dry_run = dry_run

    # --- Phase 1: automatic, no LLM -----------------------------------------

    def apply_automatic_transitions(self) -> Dict[str, List[str]]:
        """Stale/archive/reactivate authored skills by age + reuse. Returns the plan.

        Pure-ish: computes the transition for every authored skill, then (unless
        dry_run) applies archives via SkillManager. ``stale``/``reactivate`` are
        bookkeeping states recorded in curator_state; ``archive`` is the only
        filesystem effect (and it is recoverable).
        """
        from agents.task.constants import AutonomyConfig
        stale_days = AutonomyConfig.curator_stale_days()
        archive_days = AutonomyConfig.curator_archive_days()
        now = self._now()
        plan: Dict[str, List[str]] = {"stale": [], "archived": [], "reactivated": [], "kept": []}

        rows = self.usage.list_authored(created_by=_AUTHORED)
        for r in rows:
            skill_id = r["skill_id"]
            user_id = r["user_id"]
            load_count = r.get("load_count", 0) or 0
            created_at = r.get("created_at") or now
            age_days = max(0.0, (now - created_at) / _DAY_S)
            key = f"{user_id}/{skill_id}"

            # SK-F2: a background-review-authored draft always lands in
            # `.pending/` (quarantined, uncatalogued) — its load_count can
            # NEVER rise because it isn't in rules.json to be loaded from.
            # Age-based archiving would otherwise sweep up drafts nobody has
            # reviewed yet. Resolve the actual file (active wins over pending
            # if both somehow exist) and skip the row entirely while pending.
            find_file = getattr(self.sm, "_find_skill_file", None)
            if callable(find_file):
                try:
                    resolved = find_file(user_id, skill_id)
                except Exception:
                    resolved = None
                if resolved is not None and ".pending" in Path(resolved).parts:
                    continue  # pending = awaiting owner review, load_count can never rise

            if load_count > 0:
                # Reused → keep active; clear any prior stale mark.
                if self._get_mark(key) == "stale":
                    plan["reactivated"].append(key)
                    if not self.dry_run:
                        self._set_mark(key, "active")
                        self._self_mod_ev("reactivate", skill_id, user_id)
                else:
                    plan["kept"].append(key)
                continue

            # never reused
            # P2-21: skip a skill already archived on a prior tick. list_authored still
            # returns its provenance row (rows persist after archive), so without this
            # guard the weekly curator re-archived the same skill every tick — re-calling
            # delete_skill (which returns False, file already gone) and re-emitting a
            # self_modification "archive" event each time.
            if self._get_mark(key) == "archived":
                continue
            if age_days > archive_days:
                plan["archived"].append(key)
                if not self.dry_run:
                    try:
                        self.sm.delete_skill(skill_id, user_id=user_id, absorbed_into=None)
                        self._set_mark(key, "archived")
                        self._self_mod_ev("archive", skill_id, user_id)
                    except Exception as e:
                        logger.debug("curator archive failed for %s: %s", key, e)
            elif age_days > stale_days:
                plan["stale"].append(key)
                if not self.dry_run:
                    self._set_mark(key, "stale")
            else:
                plan["kept"].append(key)

        logger.info("curator phase-1: %s",
                    {k: len(v) for k, v in plan.items()})
        return plan

    @staticmethod
    def _self_mod_ev(action: str, skill_id: str, user_id: str) -> None:
        """T4-06: curator lifecycle transitions are self-modifications too —
        record them on the durable event log. Fail-open."""
        try:
            from agents.task.telemetry.self_events import emit_self_modification
            emit_self_modification(kind="skill", action=action, item_id=skill_id,
                                   user_id=user_id or "", created_by="curator",
                                   source="curator")
        except Exception as e:
            logger.debug("curator event emit skipped: %s", e)

    # --- state ---------------------------------------------------------------

    def _get_mark(self, key: str) -> Optional[str]:
        try:
            return self.usage.get_state(f"curator:{key}")
        except Exception:
            return None

    def _set_mark(self, key: str, value: str) -> None:
        try:
            self.usage.set_state(f"curator:{key}", value)
        except Exception:
            pass

    def should_run(self) -> bool:
        """Idle/interval gate: only run once per CURATOR_INTERVAL_HOURS."""
        from agents.task.constants import AutonomyConfig
        interval_s = AutonomyConfig.curator_interval_hours() * 3600.0
        last = self.usage.get_state("curator:last_run")
        if last is None:
            return True
        try:
            return (self._now() - float(last)) >= interval_s
        except (TypeError, ValueError):
            return True

    def mark_ran(self) -> None:
        self._set_mark_raw("curator:last_run", str(self._now()))

    def _set_mark_raw(self, key: str, value: str) -> None:
        try:
            self.usage.set_state(key, value)
        except Exception:
            pass

    async def run_once(self) -> Dict[str, Any]:
        """One curator pass: Phase 1 automatic transitions + episodic retention prune.
        Fail-open.

        (Stays ``async`` for the ticker contract even though Phase 1 is synchronous.)
        """
        result: Dict[str, Any] = {}
        try:
            result["transitions"] = self.apply_automatic_transitions()
            self.mark_ran()
        except Exception as e:
            logger.error("curator run failed: %s", e, exc_info=True)
            result["error"] = str(e)
        self._prune_episodes()
        return result

    def _prune_episodes(self) -> None:
        """Episodic activity-ledger retention sweep (Task 7), riding the curator's
        own cadence — never the write path. Global sweep (all tenants); fail-open
        so a memory-backend hiccup never breaks the curator tick."""
        try:
            from agents.task.constants import AutonomyConfig
            from modules.memory.registry import get_memory_registry
            prov = get_memory_registry().active()
            if prov is not None and hasattr(prov, "prune_episodes"):
                cutoff = int(self._now()) - AutonomyConfig.episodic_retention_days() * 86400
                removed = prov.prune_episodes(older_than_ts=cutoff)
                if removed:
                    logger.info("curator: pruned %d old episodes", removed)
        except Exception:
            logger.warning("episodic prune skipped", exc_info=True)


class CuratorTicker:
    """Periodically run the curator (interval-gated). Mirrors CronTicker/GoalTicker."""

    def __init__(self, curator: SkillCurator, *, check_interval_seconds: int = 3600,
                 lock_path: Optional[str] = None):
        self.curator = curator
        self.check_interval_seconds = check_interval_seconds
        self.lock_path = lock_path

    async def run_forever(self, stop_event=None) -> None:
        import asyncio
        while not (stop_event is not None and stop_event.is_set()):
            try:
                if self.curator.should_run():
                    lock = None
                    if self.lock_path:
                        from cron.scheduler import TickLock
                        lock = TickLock(self.lock_path)
                        if not lock.acquire():
                            lock = None  # another worker holds the tick this cycle; skip
                        else:
                            try:
                                await self.curator.run_once()
                            finally:
                                lock.release()
                    else:
                        await self.curator.run_once()
            except Exception as e:
                logger.error("curator tick failed: %s", e, exc_info=True)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(self.check_interval_seconds),
                    timeout=self.check_interval_seconds,
                )
            except Exception:
                pass


def build_curator_ticker(*, data_dir: str = "data", dry_run: bool = False) -> CuratorTicker:
    """Assemble the curator stack into a ticker the app lifespan can start/stop."""
    import os
    from agents.task.agent.skill_manager import get_skill_manager
    from modules.skills.skill_usage import get_skill_usage_store
    curator = SkillCurator(get_skill_manager(), get_skill_usage_store(data_dir), dry_run=dry_run)
    return CuratorTicker(curator, lock_path=os.path.join(data_dir, "curator.tick.lock"))

