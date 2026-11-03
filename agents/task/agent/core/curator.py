"""Skill curator (W5, Reference-parity curator.py; 095 W12 by last use and outcome).

Periodic maintenance of self-authored skills so the library doesn't rot: idle
authored skills go stale, then archive (recoverable, never hard-deleted); reuse
reactivates them. A skill the agent created but never reloads is noise — the curator
is what keeps "writable skills" from becoming "write-only skills".

Phase 1 (no LLM, always-on when CURATOR_ENABLED): ``apply_automatic_transitions``.

- The clock is ``last_used_at`` (a never-used skill's clock starts at ``created_at``),
  never the lifetime ``load_count`` — one load long ago is not use today (X-4).
  Idle > ``CURATOR_STALE_DAYS`` → stale; idle > ``CURATOR_ARCHIVE_DAYS`` → archive
  (via SkillManager.delete_skill → ``.archived/``); use again reactivates.
- A skill younger than :data:`INCUBATION_DAYS` is never staled or archived ("no use
  yet is no evidence").
- A skill pinned by a live cron job or goal (``payload.skills``) counts as used. A pin
  store that cannot be read stops every stale/archive this tick (an unreadable store
  is not an empty one).
- Owner-authored skills (provenance ``user``) are never auto-archived: an idle one
  files ONE owner proposal line instead (a ``self_modification`` event).

The contribution report (``contribution_report``, proposal only): per skill with at
least :data:`CONTRIBUTION_MIN_RUNS` trusted run records (``signal_source`` receipt,
owner or judge — never self/none) on each side, the useful-or-verified rate of
same-lane runs that loaded it vs that did not. A clearly worse skill files ONE owner
line; it never archives on this alone.

System/bundled skills are never touched (only skills with a provenance row).

(A Phase-2 aux-LLM "merge near-duplicate skills into umbrellas" step was scaffolded
but never had a real merge policy — it was a logged no-op. It was removed (2026-06-29
dead-loop prune); re-add it under its own flag when a concrete merge policy exists.)

Wired as a lifespan ticker (interval ``CURATOR_INTERVAL_HOURS``). Fail-open; a
dry-run mode lets the first rollout observe transitions without applying them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from core.state.machine import StateMachine  # noqa: E402

#: 098 AG-13: the curator's per-skill mark (``skill_usage`` state
#: ``curator:{uid}/{id}``). A missing mark is "never judged".
CURATOR_MARK = StateMachine(
    "curator_mark",
    states={"active", "stale", "archived", "proposed"},
    transitions={"active": {"stale", "proposed", "archived"},
                 "stale": {"active", "archived", "proposed"},
                 "archived": {"active"},
                 "proposed": {"active"}},
    initial={"active", "stale", "proposed"},
)

#: First backoff step after a failed curator pass (doubles per failure).
_BACKOFF_BASE_S = 3600.0

_AUTHORED = ["agent", "background_review"]
#: Provenance of a skill the owner wrote (console, API): never auto-archived.
_OWNER_AUTHORED = ["user"]
_DAY_S = 86400.0

#: 095 W12: days after ``created_at`` during which a skill is never staled or
#: archived. A constant beside ``CURATOR_STALE_DAYS`` (no flag).
INCUBATION_DAYS = 30
#: The contribution report's window, its minimum sample PER SIDE (runs that
#: loaded the skill, and same-lane runs that did not), and the rate gap that
#: counts as "clearly worse".
CONTRIBUTION_DAYS = 30
CONTRIBUTION_MIN_RUNS = 5
CONTRIBUTION_MARGIN = 0.25
#: The signal sources the report may learn from (``episodic_digest.SIGNAL_SOURCES``
#: minus ``self``/``none``: the agent's own word never teaches).
_TRUSTED_SIGNALS = frozenset({"receipt", "owner", "judge"})
_CONTRIBUTION_ROWS = 500


def _run_good(meta: Dict[str, Any]) -> bool:
    """A run that was useful (a receipt) or that the judge verified."""
    return meta.get("useful") is True or str(meta.get("verified") or "") == "verified"


def _run_skills(e: Any, meta: Dict[str, Any]) -> Set[str]:
    raw = meta.get("skills_loaded") or getattr(e, "skills_loaded", None) or []
    return {str(s) for s in raw if str(s or "").strip()} if isinstance(raw, (list, tuple)) else set()


def contribution_findings(skill_ids: Iterable[str], runs: Iterable[Any], *,
                          min_runs: int = CONTRIBUTION_MIN_RUNS,
                          margin: float = CONTRIBUTION_MARGIN) -> List[Dict[str, Any]]:
    """The skills that do clearly worse on the runs that load them.

    *runs* are episode records (``.meta``). Only runs with a lane and a trusted
    ``signal_source`` count. For each skill: ``with`` = runs that loaded it,
    ``without`` = runs in the SAME lanes that did not. Both sides need
    *min_runs*; a skill is reported when its rate is at least *margin* below.
    Pure; never raises on a malformed run (it is skipped)."""
    trusted: List[Tuple[str, Set[str], bool]] = []
    for e in runs or []:
        meta = getattr(e, "meta", None)
        if not isinstance(meta, dict):
            continue
        lane = str(meta.get("lane") or "")
        if not lane or str(meta.get("signal_source") or "") not in _TRUSTED_SIGNALS:
            continue
        trusted.append((lane, _run_skills(e, meta), _run_good(meta)))
    out: List[Dict[str, Any]] = []
    for sid in sorted({str(s) for s in skill_ids or []}):
        loaded = [(lane, good) for lane, skills, good in trusted if sid in skills]
        if len(loaded) < min_runs:
            continue
        lanes = {lane for lane, _ in loaded}
        without = [good for lane, skills, good in trusted if lane in lanes and sid not in skills]
        if len(without) < min_runs:
            continue
        a, b = sum(1 for _, g in loaded if g), len(loaded)
        c, d = sum(1 for g in without if g), len(without)
        if (c / d) - (a / b) >= margin:
            out.append({"skill_id": sid, "with_good": a, "with_n": b,
                        "without_good": c, "without_n": d, "lanes": sorted(lanes)})
    return out


def contribution_line(f: Dict[str, Any]) -> str:
    """The one owner line for a finding."""
    return (f"{f['skill_id']}: {f['with_good']}/{f['with_n']} useful when loaded vs "
            f"{f['without_good']}/{f['without_n']} without — patch or archive?")


class SkillCurator:
    def __init__(self, skill_manager: Any, usage_store: Any, *,
                 clock=time.time, dry_run: bool = False,
                 data_dir: Optional[str] = None):
        self.sm = skill_manager
        self.usage = usage_store
        self._now = clock
        self.dry_run = dry_run
        self.data_dir = data_dir

    # --- Phase 1: automatic, no LLM -----------------------------------------

    def _pinned_skill_keys(self) -> Optional[Set[Tuple[str, str]]]:
        """``{(user_id, skill_id)}`` pinned (``payload.skills``) by a live cron job
        (enabled, not cancelled) or a live goal. Read-only. ``None`` when a store
        that exists cannot be read; a missing store pins nothing."""
        import json
        import os
        from core.config_policy.rigs import pinned_skills
        from core.goal_vocab import LIVE_STATUSES
        from core.runtime_paths import cron_db_path, goals_db_path
        from core.sqlite_util import execute_retry
        keys: Set[Tuple[str, str]] = set()
        live = sorted(LIVE_STATUSES)
        reads = (
            (cron_db_path(self.data_dir),
             "SELECT user_id, payload FROM cron_jobs WHERE enabled=1 AND status!='cancelled'", ()),
            (goals_db_path(self.data_dir),
             "SELECT user_id, payload FROM goals WHERE status IN (%s)" % ",".join("?" * len(live)),
             tuple(live)),
        )
        for db, sql, args in reads:
            if not os.path.exists(db):
                continue
            try:
                rows = execute_retry(db, sql, args, fetch="all") or []
            except Exception:
                logger.warning("curator: pinned skills unreadable in %s", db, exc_info=True)
                return None
            for row in rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    continue
                for sid in pinned_skills(payload if isinstance(payload, dict) else {}):
                    keys.add((str(row["user_id"]), sid))
        return keys

    def apply_automatic_transitions(self) -> Dict[str, List[str]]:
        """Stale/archive/reactivate skills by idle time since last use. Returns the plan.

        Pure-ish: computes the transition for every skill with a provenance row,
        then (unless dry_run) applies archives via SkillManager. ``stale``/
        ``reactivate`` are bookkeeping states recorded in curator_state;
        ``archive`` is the only filesystem effect (and it is recoverable). An
        owner-authored skill is never archived: ``proposed`` lists the owner
        lines filed instead.
        """
        from core.config_policy import AutonomyConfig
        stale_days = AutonomyConfig.curator_stale_days()
        archive_days = AutonomyConfig.curator_archive_days()
        now = self._now()
        plan: Dict[str, List[str]] = {"stale": [], "archived": [], "reactivated": [],
                                      "kept": [], "proposed": []}
        pinned = self._pinned_skill_keys()

        rows = self.usage.list_authored(created_by=_AUTHORED + _OWNER_AUTHORED)
        for r in rows:
            skill_id = r["skill_id"]
            user_id = r["user_id"]
            created_at = r.get("created_at") or now
            last_used = r.get("last_used_at")
            age_days = max(0.0, (now - created_at) / _DAY_S)
            # X-4: the clock is the LAST use; a never-used skill's starts at creation.
            idle_days = max(0.0, (now - (last_used or created_at)) / _DAY_S)
            key = f"{user_id}/{skill_id}"
            owner_skill = r.get("created_by") in _OWNER_AUTHORED

            # SK-F2: a background-review-authored draft always lands in
            # `.pending/` (quarantined, uncatalogued) — it can NEVER be loaded
            # because it isn't in rules.json to be loaded from. Age-based
            # archiving would otherwise sweep up drafts nobody has reviewed yet.
            # Resolve the actual file (active wins over pending if both somehow
            # exist) and skip the row entirely while pending.
            find_file = getattr(self.sm, "_find_skill_file", None)
            if callable(find_file):
                try:
                    resolved = find_file(user_id, skill_id)
                except Exception:
                    resolved = None
                if resolved is not None and ".pending" in Path(resolved).parts:
                    continue  # pending = awaiting owner review, it can never be loaded

            mark = self._get_mark(key)
            is_pinned = pinned is not None and (str(user_id), skill_id) in pinned
            in_use = is_pinned or idle_days <= stale_days
            # P2-21: a skill archived on a prior tick stays archived. list_authored
            # still returns its provenance row (rows persist after archive), so
            # without this guard the curator re-archived it every tick. Only a load
            # AFTER the archive (a restored skill) brings it back.
            if mark == "archived" and not (last_used and idle_days <= stale_days):
                continue
            if in_use or age_days < INCUBATION_DAYS or pinned is None:
                # Used recently, pinned, incubating — or the pins are unreadable
                # (never stale or archive on a read we could not make).
                if mark in ("stale", "archived", "proposed") and in_use:
                    if mark != "proposed":
                        plan["reactivated"].append(key)
                    if not self.dry_run:
                        self._set_mark(key, "active")
                        if mark != "proposed":
                            self._self_mod_ev("reactivate", skill_id, user_id)
                else:
                    plan["kept"].append(key)
                continue

            if owner_skill:
                # 095 W12: the owner's skill is the owner's to archive. File ONE
                # proposal line once it is idle past the archive age; never stale it.
                if idle_days > archive_days and mark != "proposed":
                    line = (f"{skill_id}: your skill has not been used for {int(idle_days)} "
                            f"days — keep or archive?")
                    plan["proposed"].append(line)
                    if not self.dry_run:
                        self._set_mark(key, "proposed")
                        self._self_mod_ev("propose", skill_id, user_id,
                                          reason="idle", note=line)
                else:
                    plan["kept"].append(key)
                continue

            if idle_days > archive_days:
                plan["archived"].append(key)
                if not self.dry_run:
                    try:
                        moved = self.sm.delete_skill(skill_id, user_id=user_id,
                                                     created_by="curator", aged=True)
                    except Exception as e:
                        moved = False
                        logger.debug("curator archive failed for %s: %s", key, e)
                    # 098 AG-13: the mark records an effect that HAPPENED — a
                    # refused/failed archive leaves the skill active and stale,
                    # so the next pass tries again instead of skipping it.
                    if moved:
                        self._set_mark(key, "archived")
                        self._self_mod_ev("archive", skill_id, user_id)
                    else:
                        logger.warning("curator: archive of %s did not happen — "
                                       "the mark stays %s", key, mark)
                        if mark != "stale":
                            self._set_mark(key, "stale")
            else:
                plan["stale"].append(key)
                if not self.dry_run and mark != "stale":
                    self._set_mark(key, "stale")

        logger.info("curator phase-1: %s",
                    {k: len(v) for k, v in plan.items()})
        return plan

    # --- contribution report (095 W12 step 3, proposal only) -----------------

    def _recent_runs(self, user_id: str, since_ts: int) -> List[Any]:
        """The tenant's cron and goal run records since *since_ts*. Raises when
        the episode store cannot be read. Runs on the curator's worker thread."""
        from modules.memory.registry import memory_recall_episodes

        async def _read() -> List[Any]:
            out: List[Any] = []
            for kind in ("cron", "goal"):
                out += await memory_recall_episodes(
                    user_id=user_id, since_ts=since_ts, kind=kind,
                    limit=_CONTRIBUTION_ROWS, order="newest", raise_errors=True)
            return out

        return asyncio.run(_read())

    def contribution_report(self) -> List[str]:
        """File ONE owner line per skill that does clearly worse when loaded.
        Proposal only — never archives. A line is filed at most once per
        :data:`CONTRIBUTION_DAYS`. Returns the lines of this tick. Fail-open per
        tenant (an unreadable episode store files nothing)."""
        now = self._now()
        since_ts = int(now - CONTRIBUTION_DAYS * _DAY_S)
        by_user: Dict[str, List[str]] = {}
        for r in self.usage.list_authored(created_by=_AUTHORED + _OWNER_AUTHORED) or []:
            by_user.setdefault(str(r["user_id"]), []).append(str(r["skill_id"]))
        lines: List[str] = []
        for user_id, skill_ids in sorted(by_user.items()):
            try:
                runs = self._recent_runs(user_id, since_ts)
            except Exception:
                logger.warning("curator: contribution report skipped for a tenant "
                               "(run records unreadable)", exc_info=True)
                continue
            for f in contribution_findings(skill_ids, runs):
                state_key = f"contribution:{user_id}/{f['skill_id']}"
                last = self._get_mark(state_key)
                try:
                    if last is not None and now - float(last) < CONTRIBUTION_DAYS * _DAY_S:
                        continue
                except (TypeError, ValueError):
                    pass
                line = contribution_line(f)
                lines.append(line)
                if not self.dry_run:
                    self._set_mark(state_key, str(now))
                    self._self_mod_ev("propose", f["skill_id"], user_id,
                                      reason="contribution", note=line)
        if lines:
            logger.info("curator: contribution report filed %d line(s)", len(lines))
        return lines

    @staticmethod
    def _self_mod_ev(action: str, skill_id: str, user_id: str, **attrs) -> None:
        """T4-06: curator lifecycle transitions are self-modifications too —
        record them on the durable event log. A ``propose`` event carries the
        owner line in ``note``. Fail-open."""
        try:
            from core.self_events import emit_self_modification
            emit_self_modification(kind="skill", action=action, item_id=skill_id,
                                   user_id=user_id or "", created_by="curator",
                                   source="curator", **attrs)
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
            old = self.usage.get_state(f"curator:{key}")
            if old in CURATOR_MARK.states and old != value and value in CURATOR_MARK.states:
                if not CURATOR_MARK.check(old, value, ctx={"key": key}):
                    return
            self.usage.set_state(f"curator:{key}", value)
        except Exception as e:
            from core.state.machine import IllegalTransition
            if isinstance(e, IllegalTransition):
                raise

    def should_run(self) -> bool:
        """Idle/interval gate: only run once per CURATOR_INTERVAL_HOURS. After a
        failed pass (098 AG-13) the next attempt backs off ``1 h * 2**n``,
        capped at the interval — a failing pass no longer re-runs every check."""
        from core.config_policy import AutonomyConfig
        interval_s = AutonomyConfig.curator_interval_hours() * 3600.0
        try:
            failures = int(self.usage.get_state("curator:failures") or 0)
            last_try = self.usage.get_state("curator:last_attempt")
            if failures and last_try is not None:
                wait = min(interval_s, _BACKOFF_BASE_S * 2 ** (failures - 1))
                if (self._now() - float(last_try)) < wait:
                    return False
        except (TypeError, ValueError):
            pass
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

    def _repair_skill_writes(self) -> Dict[str, Any]:
        """098 MO-17: repair skill writes that stopped between their files, per
        tenant that has authored skills. Fail-open per tenant."""
        from modules.skills.repair import repair_uncommitted_skills
        out: Dict[str, Any] = {}
        users = sorted({str(r["user_id"]) for r in
                        self.usage.list_authored(created_by=_AUTHORED + _OWNER_AUTHORED) or []})
        for uid in users:
            if not hasattr(self.sm, "_user_root"):
                break
            res = repair_uncommitted_skills(self.sm, uid)
            if any(res.values()):
                out[uid] = res
        return out

    async def run_once(self) -> Dict[str, Any]:
        """One curator pass: Phase 1 automatic transitions + episodic retention prune.
        Fail-open.

        Every step below is synchronous SQLite + filesystem work, so the whole body is
        offloaded to a worker thread. It used to run inline on the shared event loop:
        the ticker awaits this coroutine on the SAME loop that serves live chat, API,
        goal and cron sessions, and `core/sqlite_util.execute_retry` does a real
        `time.sleep` (up to 15 retries x 0.15s) on write contention. One curator tick
        during a concurrent memory write could therefore stall every session in the
        process for seconds. The memory provider already routes its hot-path methods
        through `run_in_executor` for exactly this reason; the curator's own calls into
        it (prune_episodes / prune_memories / consolidate_notes) are plain `def` and
        were not covered.

        Ordering within the pass is unchanged — it runs as one sequential block, just
        off the loop.
        """
        return await asyncio.to_thread(self._run_once_blocking)

    def _run_once_blocking(self) -> Dict[str, Any]:
        """The curator pass body. Synchronous by nature (SQLite + files); callers must
        keep it off the event loop — see run_once."""
        result: Dict[str, Any] = {}
        try:
            result["repair"] = self._repair_skill_writes()
        except Exception:
            logger.warning("curator skill repair skipped", exc_info=True)
        try:
            result["transitions"] = self.apply_automatic_transitions()
            self.mark_ran()
            self._set_mark_raw("curator:failures", "0")
        except Exception as e:
            logger.error("curator run failed: %s", e, exc_info=True)
            result["error"] = str(e)
            try:
                n = int(self.usage.get_state("curator:failures") or 0) + 1
            except (TypeError, ValueError):
                n = 1
            self._set_mark_raw("curator:failures", str(n))
            self._set_mark_raw("curator:last_attempt", str(self._now()))
        try:
            result["contribution"] = self.contribution_report()
        except Exception:
            logger.warning("curator contribution report skipped", exc_info=True)
        # 097 D4: retention (episodes, memories, sessions, notes, artifacts, the
        # written-documents KB) no longer rides the curator — it runs on its own
        # always-on ticker (`agents/task/agent/core/retention.py`), so a box with
        # the curator off still keeps its retention windows.
        return result

class CuratorTicker:
    """Periodically run the curator (interval-gated). Mirrors CronTicker/GoalTicker."""

    def __init__(self, curator: SkillCurator, *, check_interval_seconds: int = 3600,
                 lock_path: Optional[str] = None):
        self.curator = curator
        self.check_interval_seconds = check_interval_seconds
        self.lock_path = lock_path

    async def _tick_once(self) -> None:
        """One curator tick: should_run gate + cross-process TickLock + run_once.
        (P4 finalization: the loop scaffolding is now IntervalTicker's — this is
        the curator-specific body it invokes, matching CronTicker/GoalTicker.)"""
        from core.autonomy_control import allows
        if not allows("curator").allowed:
            return  # 031 owner pause
        if not self.curator.should_run():
            return
        if self.lock_path:
            from cron.scheduler import TickLock
            lock = TickLock(self.lock_path)
            if not lock.acquire():
                return  # another worker holds the tick this cycle; skip
            try:
                await self.curator.run_once()
            finally:
                lock.release()
        else:
            await self.curator.run_once()

    async def run_forever(self, stop_event=None) -> None:
        # Delegate the loop to the shared IntervalTicker (consolidates cron/goal/
        # curator scheduling) instead of hand-rolling an identical loop — matches
        # how CronTicker/GoalTicker delegate.
        from core.tickers import IntervalTicker
        await IntervalTicker(self._tick_once, self.check_interval_seconds).run_forever(
            stop_event=stop_event
        )


def build_curator_ticker(*, data_dir: str = "data", dry_run: bool = False) -> CuratorTicker:
    """Assemble the curator stack into a ticker the app lifespan can start/stop."""
    import os
    from agents.task.agent.skill_manager import get_skill_manager
    from modules.skills.skill_usage import get_skill_usage_store
    curator = SkillCurator(get_skill_manager(), get_skill_usage_store(data_dir), dry_run=dry_run,
                           data_dir=data_dir)
    return CuratorTicker(curator, lock_path=os.path.join(data_dir, "curator.tick.lock"))

