"""Owner asks (§7.2b) and the hold-by-open-ask rule: create/dedup/decide/obsolete/fulfil an
ask, consume its grant, escalate spent objectives, and hold goals an OPEN ask names.

A mixin of ``GoalBoard`` (split out of ``agents/task/goals/board.py``, W9 2026-10-10). It relies
on the host for ``db_path``, ``_now``, ``_id``, ``_event``, ``get`` and ``create``; every
method opens the SAME ``goals.db`` through ``core.sqlite_util`` exactly as before. The
tables (``goals``, ``goal_events``, ``goal_edges``) are created by ``GoalBoard._init_schema``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry
from core.goal_vocab import (
    KIND_OBJECTIVE, KIND_ASK, ASK_OPEN, ASK_FULFILLED, ASK_REJECTED, ASK_OBSOLETE, OBJ_ACTIVE,
)
from agents.task.goals.board_types import (
    ASK_AUTO_UNBLOCK, Goal, _ask_goal_keys, title_similarity,
)

# The board's logger name, so a log filter on agents.task.goals.board still sees these.
logger = logging.getLogger("agents.task.goals.board")


class BoardAsksMixin:
    """``kind='ask'`` rows plus the ready-selection hold they impose."""

    # --- hold-by-open-ask (intel inbox 2026-09-16, evidence 2026-09-12) -----------
    #
    # `owner_queue` names the goal a tool approval blocks in the ask's
    # ``blocks_goal_ids`` and `decide_ask` RE-ARMS it on approval — but nothing
    # held the goal while the ask was OPEN: a run that ends "waiting on the
    # owner" is a failed run, the goal goes straight back to ``ready``, and the
    # dispatcher served it again (7 re-fires in 75 min, ~$0.42, an overclaim the
    # judge had to reject, and a ping per run). The hold lives in the ready
    # selection so every caller (dispatcher, `ready_fair`, the planner's view)
    # sees the same board. Fail-open: an unreadable ask set holds nothing.

    def _held_by_open_ask(self) -> Dict[str, str]:
        """goal_id -> ask_id for every goal an OPEN ask names (all tenants; goal
        ids are unique, so a foreign ask can only name its own goals)."""
        rows = execute_retry(
            self.db_path,
            "SELECT id, payload FROM goals WHERE kind=? AND status=? "
            "AND payload LIKE '%blocks_goal_ids%'",
            (KIND_ASK, ASK_OPEN), fetch="all",
        ) or []
        held: Dict[str, str] = {}
        for r in rows:
            try:
                ids = (json.loads(r["payload"] or "{}") or {}).get("blocks_goal_ids") or []
            except (ValueError, TypeError):
                continue
            for gid in ids:
                if gid:
                    held.setdefault(str(gid), str(r["id"]))
        return held

    def _drop_held(self, goals: List[Goal]) -> List[Goal]:
        if not goals:
            return goals
        try:
            held = self._held_by_open_ask()
        except Exception:
            logger.debug("hold-by-ask read failed (fail-open: nothing held)", exc_info=True)
            return goals
        if not held:
            return goals
        kept: List[Goal] = []
        for g in goals:
            ask_id = held.get(g.id)
            if ask_id is None:
                kept.append(g)
                continue
            try:
                self._note_hold(g.id, ask_id)
            except Exception:
                logger.debug("held_by_ask event skipped", exc_info=True)
        return kept

    def _note_hold(self, goal_id: str, ask_id: str) -> None:
        """ONE ``held_by_ask`` event per (goal, ask) — a durable, legible reason
        the goal is not moving, without a row per dispatcher tick."""
        row = execute_retry(
            self.db_path,
            "SELECT 1 FROM goal_events WHERE goal_id=? AND kind='held_by_ask' "
            "AND json_extract(payload,'$.ask_id')=? LIMIT 1",
            (goal_id, ask_id), fetch="one")
        if row is not None:
            return
        self._event(goal_id, "held_by_ask", {"ask_id": ask_id})
        logger.info("goal %s held: open owner ask %s names it (served again on a decision)",
                    goal_id[:12], ask_id[:12])

    # --- asks (§7.2b) ---------------------------------------------------------

    def add_ask_blocked_goals(self, ask_id: str, goal_ids) -> list:
        """Merge *goal_ids* into an EXISTING ask's ``blocks_goal_ids``.

        The fuzzy-dedup branch of :meth:`create_ask` has always done this inline;
        a caller that runs its OWN exact-match dedup (``owner_queue`` keys on a
        request hash and reuses the open ask) had no way to, so a re-asked
        approval kept whatever goal list it was first created with — usually
        none. The owner then approved an ask that re-armed nothing (039).

        Returns the merged list. Never raises: failing to widen the list must not
        stop an approval being requested.
        """
        wanted = [g for g in (goal_ids or []) if g]
        if not wanted:
            return []
        try:
            row = self.get(ask_id)
            if row is None:
                return []
            payload = dict(row.payload or {})
            merged = sorted(set(payload.get("blocks_goal_ids") or []) | set(wanted))
            if merged == sorted(payload.get("blocks_goal_ids") or []):
                return merged
            payload["blocks_goal_ids"] = merged
            execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                          (json.dumps(payload), ask_id))
            return merged
        except Exception:
            logger.warning("could not widen blocks_goal_ids on ask %s", ask_id,
                           exc_info=True)
            return []

    def create_ask(self, *, user_id: str, what: str, why: str = "",
                   blocks_goal_ids: Optional[List[str]] = None,
                   objective_id: Optional[str] = None,
                   extra_payload: Optional[Dict[str, Any]] = None,
                   force: bool = False) -> Goal:
        """A durable owner-facing need ("I require X from you to proceed").

        Never dispatched (``ready()`` filters ``kind='goal'``). Dedups ONLY against
        this tenant's OPEN asks — a matching one is refreshed (its dependent-goal
        set unioned) rather than respawned, so a recurring blocker stays ONE ask.

        ``extra_payload`` (Task 9 / G-2) merges additional keys into the created
        payload atomically at creation (e.g. a ``tool_approval`` ask's discriminator
        + stable request hash) — no separate post-create UPDATE, no read/write race.
        ``force=True`` skips the fuzzy title-similarity dedup above: a caller that
        already does its own EXACT-match dedup (e.g. by request hash) needs this,
        since two distinct requests can otherwise share a near-identical generic
        title (e.g. "Approve x402_request?") and get fuzzy-merged into one ask.
        """
        from core.config_policy import AutonomyConfig
        threshold = AutonomyConfig.goal_dedup_threshold()
        blocks = list(blocks_goal_ids or [])
        if not force:
            # One open ask per blocked goal (intel 10-04: the agent's own ask plus the
            # automatic "Unblock goal" ask reached the owner as two questions for one
            # block, 3x in a day). Titles differ, so the title dedup below misses it.
            new_keys = _ask_goal_keys(blocks, extra_payload)
            if new_keys:
                new_is_auto = bool((extra_payload or {}).get(ASK_AUTO_UNBLOCK))
                for a in self.asks(user_id=user_id, status=ASK_OPEN):
                    ap = dict(a.payload or {})
                    if ap.get("kind") or not (new_keys & _ask_goal_keys(
                            ap.get("blocks_goal_ids") or [], ap)):
                        continue
                    if ap.get(ASK_AUTO_UNBLOCK) and not new_is_auto:
                        # The agent's ask carries the real question (and options):
                        # it replaces the generic one and keeps holding the goal.
                        self._obsolete_ask(a.id, user_id=user_id,
                                           reason="superseded_by_agent_ask", payload=ap)
                        blocks = sorted(set(blocks) | set(ap.get("blocks_goal_ids") or []))
                        break
                    ap["blocks_goal_ids"] = sorted(
                        set(ap.get("blocks_goal_ids") or []) | set(blocks))
                    execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                                  (json.dumps(ap), a.id))
                    self._event(a.id, "ask_refreshed", {"what": what[:200],
                                                        "dedup": "same_goal"})
                    return self.get(a.id)
        if not force and threshold > 0:
            for a in self.asks(user_id=user_id, status=ASK_OPEN):
                if title_similarity(what, a.title) >= threshold:
                    merged = sorted(set((a.payload or {}).get("blocks_goal_ids", [])) | set(blocks))
                    payload = dict(a.payload or {})
                    payload["blocks_goal_ids"] = merged
                    execute_retry(self.db_path, "UPDATE goals SET payload=? WHERE id=?",
                                  (json.dumps(payload), a.id))
                    self._event(a.id, "ask_refreshed", {"what": what[:200]})
                    return self.get(a.id)
        payload: Dict[str, Any] = {"blocks_goal_ids": blocks}
        if extra_payload:
            payload.update(extra_payload)
        return self.create(user_id=user_id, title=what, body=why, kind=KIND_ASK,
                           status=ASK_OPEN, parent_id=objective_id, force=True,
                           payload=payload, actor="none")

    #: ``payload.kind`` discriminator for the budget-exhaustion ask below, so the
    #: "does one already exist" check is an EXACT match on a stable key rather
    #: than fuzzy title similarity against unrelated asks.
    ASK_KIND_OBJECTIVE_SPENT = "objective_budget_spent"

    def escalate_spent_objectives(self, *, user_id: str) -> List[str]:
        """Raise ONE open ask per active objective that has spent its goal budget.

        ``_check_objective_budget`` refuses the child and tells the AGENT to raise
        an ask, and the planner prompt repeats it — but both are prompt-shaped, so
        whether the owner ever hears about it depends on the model complying. Live
        evidence (prod, 2026-08-28): two of four active objectives sat at 25/25 for
        days, the planner could open no work on either, and no ask was ever raised.
        A budget-exhausted objective is a STALLED objective; the owner learning
        about it must not be model-dependent.

        Idempotent: an existing OPEN ask carrying this payload kind for the same
        objective is left alone (no refresh churn). Returns the ask ids created.
        Fail-open per objective — this runs on the dispatch tick and must never be
        the reason a tick fails.
        """
        created: List[str] = []
        try:
            objectives = [o for o in self.list(user_id=user_id, status=OBJ_ACTIVE,
                                               limit=200)
                          if o.kind == KIND_OBJECTIVE]
            open_asks = self.asks(user_id=user_id, status=ASK_OPEN)
        except Exception:
            logger.debug("spent-objective escalation skipped (board read failed)",
                         exc_info=True)
            return created
        spent_asks = {(a.payload or {}).get("objective_id"): a
                      for a in open_asks
                      if (a.payload or {}).get("kind") == self.ASK_KIND_OBJECTIVE_SPENT}
        for o in objectives:
            try:
                budget = self.objective_budget(o)
                spent = False
                live_children: List[Goal] = []
                if budget > 0:  # a stream is uncapped by design
                    live_children = self.children_of(user_id, o.id)
                    spent = len(live_children) >= budget
                existing = spent_asks.get(o.id)
                if existing is not None:
                    if not spent:
                        # The NEED went away — the objective became a stream, its
                        # budget was raised, or children were cancelled. An ask
                        # that outlives its condition is a permanent, misleading
                        # "owner decision pending" (prod 2026-08-29: the planner
                        # cited two such asks as REAL BLOCKERs every hour).
                        self._obsolete_ask(existing.id, user_id=user_id,
                                           reason="objective_no_longer_spent",
                                           payload=dict(existing.payload or {}))
                    continue
                if not spent:
                    continue
                ask = self.create_ask(
                    user_id=user_id,
                    what=f"Objective at its goal budget: {o.title}",
                    why=(f"'{o.title}' has {len(live_children)}/{budget} live goals — its "
                         f"lifetime budget. The planner can open NO further work on it, so "
                         f"it is stalled, not finished. Decide one: drop it "
                         f"(`/goal objective drop {o.id}`) if it is done; raise its "
                         f"`goal_budget` if it should continue; or convert it to a stream "
                         f"for standing recurring work."),
                    blocks_goal_ids=[g.id for g in live_children[:20]],
                    objective_id=o.id,
                    extra_payload={"kind": self.ASK_KIND_OBJECTIVE_SPENT,
                                   "objective_id": o.id,
                                   "live": len(live_children), "budget": budget},
                    force=True)  # exact-match dedup above; skip fuzzy title merging
                if ask is not None:
                    created.append(ask.id)
                    self._event(o.id, "objective_budget_spent",
                                {"live": len(live_children), "budget": budget,
                                 "ask_id": ask.id})
            except Exception:
                logger.debug("spent-objective escalation failed for %s", o.id,
                             exc_info=True)
        return created

    def asks(self, *, user_id: str, status: Optional[str] = None) -> List[Goal]:
        sql = "SELECT * FROM goals WHERE kind=? AND user_id=?"
        params: List[Any] = [KIND_ASK, user_id]
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def decide_ask(self, ask_id: str, *, user_id: str, approved: bool,
                   answer: Optional[str] = None,
                   answer_via: Optional[str] = None) -> tuple:
        """Record an owner decision (approve or reject) on an OPEN ask.

        ``answer`` is the owner's free-text reply (2026-09-21, interface audit
        A27): it is stamped on the ask as ``payload.answer`` and, on approval,
        carried into each dependent goal's ``payload.owner_unblocked.answer`` so
        the retry prompt tells the run WHAT the owner said, not only THAT it
        was unblocked. Bounded to 2000 chars; an empty answer writes nothing.

        Generalizes :meth:`fulfill_ask` (Task 9 / G-2 — a ``tool_approval`` ask
        needs a real reject outcome, not just fulfilled/still-open). Approving
        flips BLOCKED dependent goals back to ready (the original unblock hop);
        rejecting never touches dependent goals. The decision is also stamped
        into the ask's own ``payload.decision`` (``"approved"``/``"rejected"``)
        so a poller (e.g. ``OwnerQueueApprover``) can read the outcome straight
        off the row without a second status vocabulary. Tenant-scoped CAS —
        only an OPEN ask for THIS ``user_id`` transitions. Returns
        ``(ok, unblocked_count)``.
        """
        now = self._now()
        ask = self.get(ask_id)
        payload = dict(ask.payload or {}) if ask else {}
        payload["decision"] = "approved" if approved else "rejected"
        answer_text = (answer or "").strip()[:2000]
        if answer_text:
            payload["answer"] = answer_text
            # H04: WHERE the text was recorded (an owner seat verb vs an agent
            # turn's `owner_ask(answer=)`) — rendered as provenance, never trust.
            payload["answer_via"] = (answer_via or "owner seat").strip()[:120]
        new_status = ASK_FULFILLED if approved else ASK_REJECTED
        rc = execute_retry(
            self.db_path,
            "UPDATE goals SET status=?, completed_at=?, payload=? "
            "WHERE id=? AND kind=? AND status=? AND user_id=?",
            (new_status, now, json.dumps(payload), ask_id, KIND_ASK, ASK_OPEN, user_id),
        )
        if rc != 1:
            return (False, 0)
        self._event(ask_id, "ask_fulfilled" if approved else "ask_rejected", {})
        if approved and str(payload.get("rail_id") or "").startswith("cron:"):
            # 2026-10-07: an ask a CRON run raised has no goal to flip; pull the
            # job to the next tick so the answer is acted on now (rail_answers).
            from agents.task.goals.rail_answers import rearm_cron_rail
            rearm_cron_rail(self, self.get(ask_id))
        unblocked = 0
        if approved:
            for gid in (ask.payload or {}).get("blocks_goal_ids", []) if ask else []:
                # 2026-07-14 night-2: stamp the fulfillment onto the goal payload so
                # the retry prompt (context.build_goal_run_task) tells the run the
                # blocker was FIXED — without this the agent reads only the old
                # failure ledger and declares BLOCKED from memory without retrying.
                dep = self.get(gid)
                dep_payload = dict(dep.payload or {}) if dep else {}
                dep_payload["owner_unblocked"] = {"ts": now, "ask_id": ask_id}
                if answer_text:
                    dep_payload["owner_unblocked"]["answer"] = answer_text
                    dep_payload["owner_unblocked"]["answer_via"] = payload.get("answer_via", "")
                # T2.1 final-review Fix 1: an ask-fulfillment unblock is also an
                # owner reset — clear the stale block_kind (see unblock()'s
                # docstring for the full rationale). provider_requeues /
                # provider_retry_exhausted are untouched (only this ONE key is
                # popped) so the requeue-cap ledger survives the episode.
                dep_payload.pop("block_kind", None)
                rc2 = execute_retry(
                    self.db_path,
                    """UPDATE goals SET status='ready', consecutive_failures=0,
                          last_failure_error=NULL, claim_lock=NULL, claim_expires=NULL,
                          payload=?
                        WHERE id=? AND kind='goal' AND status='blocked' AND user_id=?""",
                    (json.dumps(dep_payload), gid, user_id),
                )
                if rc2 == 1:
                    self._event(gid, "unblocked_by_ask", {"ask_id": ask_id})
                    unblocked += 1
        return (True, unblocked)

    def _release_asks(self, goal_id: str, reason: str) -> None:
        """Close the owner asks a now-terminal goal was blocking. Fail-open.

        The tenant is read off the goal row, so every terminal transition can
        release its asks without threading a user_id through call sites that
        never had one. Bookkeeping — it must never fail the transition itself.
        """
        try:
            goal = self.get(goal_id)
            if goal is not None and goal.user_id:
                self.close_asks_for_goal(goal.user_id, goal_id, reason=reason)
        except Exception:
            logger.debug("ask release skipped for %s", goal_id, exc_info=True)

    def close_asks_for_goal(self, user_id: str, goal_id: str, *,
                            reason: str = "goal_resolved") -> int:
        """Release *goal_id* from every OPEN ask, closing any ask left with none.

        Called when a goal reaches a terminal state (done / cancelled): the owner
        no longer has a decision to make about it. An ask blocking SEVERAL goals
        stays open until the last one resolves — the need is still real for the
        others.

        Only OPEN asks move, and only this tenant's, so an owner-answered ask is
        never reopened or re-closed. Returns the number of asks closed.
        """
        if not (user_id and goal_id):
            return 0
        closed = 0
        for ask in self.asks(user_id=user_id, status=ASK_OPEN):
            payload = dict(ask.payload or {})
            blocks = list(payload.get("blocks_goal_ids") or [])
            if goal_id not in blocks:
                continue
            remaining = [g for g in blocks if g != goal_id]
            payload["blocks_goal_ids"] = remaining
            if remaining:
                # Still needed by another goal — record the release, stay open.
                execute_retry(
                    self.db_path,
                    "UPDATE goals SET payload=? WHERE id=? AND kind=? AND user_id=?",
                    (json.dumps(payload), ask.id, KIND_ASK, user_id),
                )
                continue
            if self._obsolete_ask(ask.id, user_id=user_id, reason=reason,
                                  payload=payload, event_extra={"goal_id": goal_id}):
                closed += 1
        return closed

    def obsolete_ask(self, ask_id: str, *, user_id: str,
                     reason: str = "resolved") -> bool:
        """Close one OPEN ask because the NEED went away on its own.

        Distinct from :meth:`decide_ask` on purpose: fulfilled/rejected record an
        OWNER decision, and marking a self-resolving need as "the owner fulfilled
        it" would be a false record. Tenant-scoped CAS on ``status='open'``, so an
        already-answered ask is never reopened or re-closed.
        """
        ask = self.get(ask_id)
        if ask is None or ask.kind != KIND_ASK or ask.user_id != user_id:
            return False
        return self._obsolete_ask(ask_id, user_id=user_id, reason=reason,
                                  payload=dict(ask.payload or {}))

    def _obsolete_ask(self, ask_id: str, *, user_id: str, reason: str,
                      payload: Dict[str, Any],
                      event_extra: Optional[Dict[str, Any]] = None) -> bool:
        payload = dict(payload)
        payload["decision"] = "obsolete"
        payload["obsolete_reason"] = reason
        rc = execute_retry(
            self.db_path,
            "UPDATE goals SET status=?, completed_at=?, payload=? "
            "WHERE id=? AND kind=? AND status=? AND user_id=?",
            (ASK_OBSOLETE, self._now(), json.dumps(payload), ask_id, KIND_ASK,
             ASK_OPEN, user_id),
        )
        if rc != 1:
            return False
        ev = {"reason": reason}
        ev.update(event_extra or {})
        self._event(ask_id, "ask_obsolete", ev)
        return True

    def fulfill_ask(self, ask_id: str, *, user_id: str) -> tuple:
        """Mark an ask fulfilled and flip its BLOCKED dependent goals back to ready.

        The unblock hop: each dependent goal that is currently ``blocked`` gets a
        clean failure counter and re-enters the dispatch queue. Tenant-scoped CAS.
        Returns ``(ok, unblocked_count)``. Thin wrapper over :meth:`decide_ask`
        (``approved=True``) — kept as its own method since it's the established
        public name (`polyrob owner fulfill`, Telegram ``/fulfill``).
        """
        return self.decide_ask(ask_id, user_id=user_id, approved=True)

    def consume_ask_grant(self, ask_id: str) -> bool:
        """Atomically consume a FULFILLED ask's one-shot grant flag (Task 9 / G-2).

        A ``tool_approval`` ask is created with ``payload.grant_consumed=false``
        (see :class:`tools.controller.approval_queue.OwnerQueueApprover`). Once
        it is approved (fulfilled) AFTER the requester already timed out and gave
        up, the NEXT identical request may redeem it exactly once. The CAS is a
        single atomic ``UPDATE ... WHERE id=? AND
        json_extract(payload, '$.grant_consumed')=0`` — the same atomic-claim
        shape ``modules/x402/invoicing.py::claim_wake`` uses (no
        read-modify-write race between two concurrent redeemers). Returns True
        for the caller that won the claim; False otherwise (already consumed,
        no such ask, or the payload doesn't carry the flag).

        Task 9b (2026-08-22): this used to do a string ``REPLACE`` on the
        machine-written ``"grant_consumed": false`` token, guarded by a
        ``LIKE`` match on the same spaced literal — the exact bug class Task 9
        fixed in ``modules/x402/invoicing.py`` (SQLite's ``json_set``
        re-serializes the WHOLE JSON blob COMPACTLY, which a spaced-literal
        match then silently stops matching). No ``json_set`` currently touches
        ``goals.payload`` anywhere in this codebase, so this was LATENT, not
        reachable — fixed anyway to remove the landmine before some future
        dedup/backfill routine (mirroring
        ``modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index``)
        adds one. ``json_set(..., json('true'))`` (NOT the bare number ``1``)
        keeps the stored shape a genuine JSON boolean, matching
        ``claim_wake``'s convention.
        """
        rc = execute_retry(
            self.db_path,
            """UPDATE goals SET payload = json_set(payload, '$.grant_consumed', json('true'))
                WHERE id=? AND kind=? AND json_extract(payload, '$.grant_consumed')=0""",
            (ask_id, KIND_ASK),
        )
        return rc == 1
