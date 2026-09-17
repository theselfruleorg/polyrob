"""``owner_queue`` — a durable, remote-capable ``ApprovalProvider`` (Task 9 / G-2).

Closes the gap where the only approver an operator could actually wire was a
blocking stdin prompt (`tools/controller/approval_interactive.py`) — useless on a
headless prod box — while Telegram's `/approve` verb was self-evolution-only. This
provider makes the SAME `/approve` (and `polyrob owner promote`) surface resolve a
gated tool call too, by queuing it as a durable ask instead of blocking a thread.

Reuses EXISTING seams — no parallel mechanisms:
  - the durable asks store the goal-board budget/blocker escalation gates already
    write to (`agents.task.goals.board.GoalBoard` — ``kind='ask'`` rows in
    ``goals.db``; `create_ask`/`asks`/`decide_ask`/`consume_ask_grant`);
  - the ONE owner-notification rail (`core.surfaces.user_delivery.deliver_user_message`
    — the same primitive `core.self_evolution.push_owner_message` and the goal
    dispatcher's budget-gate escalation push already ride);
  - the async approval hook pipeline (`tools/controller/approval.py::make_approval_hook`),
    which bounds ``provider.request()`` with ``asyncio.wait_for(..., APPROVAL_TIMEOUT_SEC)``
    and denies on both a real ``False`` and a timeout/cancellation.

Flow (``OwnerQueueApprover.request``):
  1. Compute a stable **request hash** over ``(tool_name, normalized params, tenant)``.
     An identical retry (same hash) always resolves to the SAME durable ask/grant —
     it never spams a second ask or a second owner notification.
  2. Check for an unconsumed, unexpired ONE-SHOT GRANT left by a post-timeout owner
     decision on an identical prior request (`GoalBoard.consume_ask_grant`, TTL
     ``APPROVAL_GRANT_TTL_HOURS``) — consume it atomically and return True without
     re-queuing.
  3. Otherwise find-or-create an OPEN ``tool_approval`` ask carrying the hash, push
     ONE owner notification (only when the ask is newly created — a retried
     identical request reuses the ask and does not re-notify), then poll the ask
     row for a decision until the OUTER ``asyncio.wait_for`` cancels this coroutine.
     Approved -> True, rejected -> False.
  4. On cancellation (timeout), the ask is left OPEN/visible for the owner; a LATER
     decision (`/approve`/`/reject` — Telegram, or ``polyrob owner promote/reject
     tool_approval <id>``) is recorded on the SAME ask, becoming the one-shot grant
     a retried identical request consumes per step 2.

Cancellation-safety (UP-04 contract): the poll loop only ever ``await``s
``asyncio.sleep`` — no background task/thread/lock is held — so the outer
``CancelledError`` unwinds cleanly through a single ``finally`` (tracked via
``_active_polls`` so tests can assert it actually ran).
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from tools.controller.approval import ApprovalProvider, register_approval_provider

logger = logging.getLogger(__name__)

TOOL_APPROVAL_ASK_KIND = "tool_approval"
TAP_PREFIX = "tap-"
DEFAULT_POLL_INTERVAL_SEC = 2.0


def tap_display_id(ask_id: str) -> str:
    """The namespaced id a `tool_approval` ask is SHOWN as (Telegram `/pending`,
    `polyrob owner pending`) — disambiguates from a self-evolution proposal id in
    the single-id `/approve <id>` / `/reject <id>` dispatch (no explicit `kind`)."""
    return f"{TAP_PREFIX}{ask_id}"


def strip_tap_prefix(display_id: str) -> Optional[str]:
    """Inverse of :func:`tap_display_id`. None when `display_id` isn't tap-prefixed."""
    if isinstance(display_id, str) and display_id.startswith(TAP_PREFIX):
        real = display_id[len(TAP_PREFIX):]
        return real or None
    return None


def _normalize_params(params: Any) -> Dict[str, Any]:
    if params is None:
        return {}
    if hasattr(params, "model_dump"):
        try:
            params = params.model_dump()
        except Exception:
            pass
    if not isinstance(params, dict):
        return {"_repr": str(params)}
    return params


def compute_request_hash(tool_name: str, params: Any, user_id: str) -> str:
    """Stable id for "this tenant asking to run this tool with these params" — an
    exact repeat (a retry after timeout, or a genuinely identical follow-up call)
    resolves to the SAME hash so it shares one durable ask / one one-shot grant."""
    normalized = _normalize_params(params)
    try:
        blob = json.dumps(normalized, sort_keys=True, default=str)
    except Exception:
        blob = str(normalized)
    raw = f"{tool_name}:{user_id or ''}:{blob}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _params_summary(params: Dict[str, Any], *, max_len: int = 300) -> str:
    try:
        text = json.dumps(params, sort_keys=True, default=str)
    except Exception:
        text = str(params)
    return text[:max_len]


def _goals_db_path(home_dir: Any) -> str:
    # WS-3: one shared resolver — {home_dir}/goals.db, else the data home (never a
    # relative "data" under the cwd).
    from core.runtime_paths import goals_db_path
    return goals_db_path(home_dir or None)


def list_pending_tool_approvals(board: Any, user_id: str) -> List[Dict[str, Any]]:
    """Open ``tool_approval`` asks for ``user_id``, shaped like
    ``core.self_evolution.list_pending``'s items (``kind``/``id``/``chars``/``preview``)
    so a caller (Telegram ``/pending``, ``polyrob owner pending``) can concatenate the
    two lists and dispatch on them uniformly."""
    from agents.task.goals.board import ASK_OPEN
    out: List[Dict[str, Any]] = []
    for a in board.asks(user_id=user_id, status=ASK_OPEN):
        payload = a.payload or {}
        if payload.get("ask_kind") != TOOL_APPROVAL_ASK_KIND:
            continue
        # 030 WS-E1 (C-5): action-first preview, not the machine string
        # "tool=… params={json} session=…" triple-truncated downstream.
        try:
            from tools.controller.grant_card import render_pending_preview
            preview = render_pending_preview(
                payload.get("tool_name") or (a.title or ""),
                payload.get("params_summary") or "")
        except Exception:
            preview = (a.body or a.title or "")[:160]
        out.append({
            "kind": TOOL_APPROVAL_ASK_KIND,
            "id": tap_display_id(a.id),
            "chars": len(preview),
            "preview": preview,
        })
    return out


class PendingSet:
    """The owner's whole approval queue, plus what could not be read.

    ``items`` is the union every owner seat lists AND decides over.
    ``unavailable`` names each source that failed, so a partial queue is
    reported as partial. A status surface may be incomplete; it may not be
    confident and wrong. A queue that silently drops the source holding the
    owner's payment approval is the second kind.
    """

    __slots__ = ("items", "unavailable")

    def __init__(self, items: List[Dict[str, Any]], unavailable: List[str]):
        self.items = items
        self.unavailable = unavailable

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __bool__(self) -> bool:
        return bool(self.items) or bool(self.unavailable)

    def degraded_line(self) -> str:
        """One line naming the unreadable sources, or ``""`` when all were read."""
        if not self.unavailable:
            return ""
        return ("\u26a0 I could not read " + ", ".join(self.unavailable)
                + " \u2014 there may be more waiting than this list shows.")


def all_pending(*, user_id: str, home_dir: Any, instance_id: str,
                board: Any = None, correspondent_registry: Any = None,
                skill_manager: Any = None) -> PendingSet:
    """The ONE pending union: self-evolution proposals + queued tool/spend
    approvals + pending correspondent bindings.

    2026-09-15. Three seats built this join by hand (``polyrob owner pending``,
    Telegram ``/pending``, the webview review page) and a fourth \u2014 the bare
    ``/approve`` shortcut and ``/approve all`` \u2014 read only the FIRST source.
    So with three skills and one payment ask waiting, ``/approve`` offered three
    and hid the payment; with only a payment ask waiting it answered "Nothing
    pending \u2014 there is nothing waiting on you", a confident lie over real work;
    and ``/approve all``, advertised as "everything at once", left two queues
    untouched. Listing and deciding now read the same function.

    Each source is read in its own try: one unreadable store must not hide the
    other two. What failed is NAMED in ``PendingSet.unavailable``, never dropped.
    """
    import os

    from core import self_evolution

    items: List[Dict[str, Any]] = []
    unavailable: List[str] = []

    try:
        items += self_evolution.list_pending(user_id, home_dir=home_dir,
                                             instance_id=instance_id,
                                             skill_manager=skill_manager)
    except Exception:
        logger.warning("all_pending: self-evolution proposals unreadable", exc_info=True)
        unavailable.append("my own proposals")

    try:
        if board is None:
            from agents.task.goals.board import GoalBoard
            board = GoalBoard(_goals_db_path(home_dir))
        items += list_pending_tool_approvals(board, user_id)
    except Exception:
        logger.warning("all_pending: tool approvals unreadable", exc_info=True)
        unavailable.append("queued tool + spend approvals")

    try:
        from core.surfaces.owner_admin import pending_correspondent_items
        if correspondent_registry is None:
            from core.surfaces.correspondents import CorrespondentRegistry
            correspondent_registry = CorrespondentRegistry(
                os.path.join(str(home_dir), "correspondents.db"))
        items += pending_correspondent_items(correspondent_registry, user_id)
    except Exception:
        logger.warning("all_pending: correspondent bindings unreadable", exc_info=True)
        unavailable.append("pending contacts")

    return PendingSet(items, unavailable)


def resolve_pending_target(target: str, pending: Any) -> Optional[Dict[str, Any]]:
    """Map what the owner tapped or typed back to ONE item of the live queue.

    Accepts the tappable alias (``p-a1b2c3``), the displayed id, or a
    ``kind:id`` pair. Returns ``None`` when it names none or more than one \u2014
    refusing is the only honest answer, because acting on either of two matches
    decides a proposal the owner never looked at.
    """
    from core.self_evolution import resolve_pending_alias

    items = list(getattr(pending, "items", pending) or [])
    want = str(target or "").strip()
    if not want:
        return None
    hit = resolve_pending_alias(want, items)
    if hit is not None:
        return hit
    exact = [it for it in items if str(it.get("id", "")) == want]
    if len(exact) == 1:
        return exact[0]
    qualified = [it for it in items
                 if f"{it.get('kind', '')}:{it.get('id', '')}" == want]
    return qualified[0] if len(qualified) == 1 else None


def decide_pending(kind: str, item_id: Any, *, approve: bool, user_id: str,
                   home_dir: Any, instance_id: str, board: Any = None,
                   correspondent_registry: Any = None,
                   task_agent: Any = None) -> tuple:
    """Record the owner's decision on ONE item of the union. ``(ok, message)``.

    The union holds three kinds of thing and each has its own decider. Routing
    lives HERE so no seat can disagree with another about what a decision
    means — which they did: the REPL's own Inbox advertised
    ``/pending approve tool_approval <id>``, and its handler passed that kind
    straight to the self-evolution promoter, which answered "unknown pending
    kind". The remedy pointed at a command the seat could not run.
    """
    import os

    from core import self_evolution

    item_id = str(item_id or "")
    if kind == TOOL_APPROVAL_ASK_KIND:
        if board is None:
            from agents.task.goals.board import GoalBoard
            board = GoalBoard(_goals_db_path(home_dir))
        # 030 WS-E3: pass the live agent so an approval wakes the originating
        # session (resume-on-grant) instead of waiting for a byte-identical
        # retry to happen by luck.
        return decide_tool_approval(board, item_id, user_id=user_id,
                                    approved=approve, task_agent=task_agent)
    if kind == "correspondent":
        surface, _, address = item_id.partition(":")
        if not surface or not address:
            return False, f"'{item_id}' is not a <surface>:<address> contact"
        if correspondent_registry is None:
            from core.surfaces.correspondents import CorrespondentRegistry
            correspondent_registry = CorrespondentRegistry(
                os.path.join(str(home_dir), "correspondents.db"))
        try:
            ok = bool(correspondent_registry.approve(surface=surface, address=address,
                                                     user_id=user_id)
                      if approve else
                      correspondent_registry.reject(surface=surface, address=address,
                                                    user_id=user_id))
        except Exception as e:
            return False, f"contact {item_id} could not be decided: {e}"
        if not ok:
            return False, f"no pending contact {item_id}"
        return True, (f"contact {item_id} approved — their replies now reach me as data"
                      if approve else
                      f"contact {item_id} rejected — the binding is tombstoned")
    fn = self_evolution.promote if approve else self_evolution.reject
    return fn(kind, item_id, user_id=user_id, home_dir=home_dir,
              instance_id=instance_id)


def decide_all_pending(*, approve: bool, user_id: str, home_dir: Any,
                       instance_id: str, board: Any = None,
                       correspondent_registry: Any = None,
                       task_agent: Any = None) -> tuple:
    """Decide the WHOLE queue. ``(ok_count, fail_count, messages)``.

    "All" used to mean the self-evolution third of it on every seat, while the
    listing directly above showed all three — so a queued payment approval or a
    pending contact survived an "approve all" with no trace.

    Iterates a SNAPSHOT: deciding a single-slot kind mutates the live set.
    """
    pending = all_pending(user_id=user_id, home_dir=home_dir,
                          instance_id=instance_id, board=board,
                          correspondent_registry=correspondent_registry)
    msgs: List[str] = []
    ok_n = fail_n = 0
    for it in list(pending.items):
        kind, item_id = it.get("kind", ""), it.get("id")
        try:
            ok, msg = decide_pending(kind, item_id, approve=approve, user_id=user_id,
                                     home_dir=home_dir, instance_id=instance_id,
                                     board=board,
                                     correspondent_registry=correspondent_registry,
                                     task_agent=task_agent)
        except Exception as e:  # one bad item must not abandon the rest
            ok, msg = False, f"{kind}:{item_id} failed: {e}"
        ok_n, fail_n = (ok_n + 1, fail_n) if ok else (ok_n, fail_n + 1)
        # \u26a0\ufe0f The mark is bound OUTSIDE the f-string: a backslash escape inside an
        # f-string expression is a SyntaxError before 3.12, and pyproject floors
        # this project at 3.11.
        mark = "\u2713" if ok else "\u2717"
        msgs.append(f"{mark} {kind}:{item_id} \u2014 {msg}")
    if pending.unavailable:
        msgs.append(pending.degraded_line())
    return ok_n, fail_n, msgs


def _approval_wake_text(tool_name: str, display_id: str) -> str:
    """The ONE resume-on-grant wake message (in-process AND cross-process rails
    read it — extracted so the two cannot drift). It promises a live grant, never
    a retry a money verb would refuse on a forged turn."""
    return (f"Owner approved {tool_name} ({display_id}). The one-shot grant is "
            f"live for the next identical attempt. If this turn is a self-wake and "
            f"{tool_name} moves money, the guard will still refuse it — say the "
            f"grant is live and wait for the owner's own request. Do not retry in "
            f"a loop.")


def _owns_session_locally(task_agent: Any, session_id: str) -> bool:
    """True iff ``task_agent`` lives in THIS process AND the session's
    orchestrator is RESIDENT here — the only case an in-process self-wake is
    correct. On prod the console holds a monitoring ``TaskAgent`` but the session
    is owned by the SEPARATE agent process (not resident here) -> False -> a
    durable cross-process wake row instead. Backend-independent:
    ``route_session().is_local`` means resident-here for both the in-process and
    the sqlite session registries. Fail-CLOSED to False (durable row): reaching
    the owning process is always safe; recreating a remote session in the wrong
    process is the hazard the ruling forbids."""
    if task_agent is None:
        return False
    route_fn = getattr(task_agent, "route_session", None)
    if not callable(route_fn):
        return False
    try:
        return bool(getattr(route_fn(session_id), "is_local", False))
    except Exception:
        return False


def _wake_queue_for_board(board: Any):
    """The durable wake queue that lives BESIDE this board's ``goals.db`` (the
    same data home), so the deciding process and the owning process resolve the
    SAME file (and a tmp board keeps tests isolated)."""
    import os

    from core.wake_queue import get_wake_queue
    db_path = getattr(board, "db_path", None)
    if db_path:
        return get_wake_queue(
            os.path.join(os.path.dirname(os.path.abspath(db_path)), "wakes.db"))
    return get_wake_queue()


def decide_tool_approval(board: Any, display_id: str, *, user_id: str,
                         approved: bool, task_agent: Any = None) -> tuple:
    """Resolve a (possibly ``tap-``-prefixed) ask id back to the real ask id and
    record the owner's decision. Returns ``(ok, message)`` — the shared handler
    behind Telegram `/approve` `/reject` and `polyrob owner promote/reject
    tool_approval`.

    030 WS-E3 / 043 W10 (resume-on-grant): a POST-timeout approval used to rely
    on the agent happening to retry a byte-identical call within the grant TTL.
    Now an approval wakes the originating session so the grant is redeemed
    immediately, through the session's OWNING process:
      * same process (``task_agent`` passed) -> wake in-process via the self-wake
        rail, fire-and-forget;
      * a DIFFERENT process (prod: the CONSOLE decides, the AGENT owns the
        session) -> a durable wake row (``core/wake_queue.py``) the owning
        process's autonomy tick drains — ``deliver_self_wake`` refuses a remote
        session, so the wake must run where the session lives, not here.
    A goal-blocking approval writes NO wake either way (``decide_ask`` re-arms the
    goal and the dispatcher redeems it — a forged wake turn may not spend). The
    ask row flips regardless; both wake paths are fail-open niceties.
    """
    real_id = strip_tap_prefix(display_id) or display_id
    row = None
    try:
        row = board.get(real_id)  # unscoped on purpose: decide_ask below is the tenant gate
    except Exception:
        row = None
    ok, _ = board.decide_ask(real_id, user_id=user_id, approved=approved)
    if not ok:
        return False, f"no open tool-approval request '{display_id}'"
    verb = "approved" if approved else "rejected"
    # W10 (043): resume-on-grant wakes the session that asked. The wake MUST
    # re-enter through the session's OWNING process — `deliver_self_wake` refuses
    # a remote session by design. So: same process (a live `task_agent`) -> wake
    # in-process now; a DIFFERENT process (prod Rob #1: the console decides, the
    # agent owns the session) -> write a DURABLE wake row the owning process's
    # autonomy tick drains. Either way the ask row already flipped above (d).
    if approved and row is not None:
        try:
            payload = getattr(row, "payload", None) or {}
            session_id = payload.get("session_id")
            tool_name = payload.get("tool_name") or "the gated action"
            if payload.get("blocks_goal_ids"):
                # ⚠️ DO NOT wake when a goal is being re-armed (carve-out, both
                # rails).
                #
                # Live on prod 2026-09-12: the owner approved a bridge, a self-wake
                # said "Retry it now", and `tx_guard` step 2 refused it —
                # "forged/autonomous turns cannot move funds, even with the grant".
                # A self-wake turn is structurally barred from spending, so
                # resume-on-grant instructed the agent to do the one thing that
                # turn cannot do, and burned the attempt doing it.
                #
                # `decide_ask` above has ALREADY flipped the goal back to ready.
                # The dispatcher will run it as a genuine autonomous goal turn,
                # which MAY spend and WILL redeem the grant. That is the redemption
                # path; a wake here (in ANY process) only races it with a turn that
                # cannot finish, and risks stamping `turn_kind=self_wake` onto the
                # goal run's own session when the message drains there.
                logger.info(
                    "owner_queue: %s approved — leaving redemption to the re-armed "
                    "goal(s) %s; no wake (a forged turn may not spend)",
                    display_id, payload.get("blocks_goal_ids"))
            elif payload.get("cron_job_id"):
                # A CRON run asked (2026-09-18). Same shape as the goal carve-out:
                # the session that asked has FINISHED, and a self-wake into it is
                # a forged turn the money guard refuses — live on prod 2026-09-17
                # 16:14, the owner tapped approve on a buyback tranche, the wake
                # said "grant is live, wait for the owner", and the agent asked
                # the owner to trigger a trade the owner had just approved. So:
                # no wake. Re-arm the JOB instead — its next tick is a genuine
                # cron turn, which MAY spend and consumes the one-shot grant.
                job_id = str(payload.get("cron_job_id"))
                if _rearm_cron_job(board, job_id):
                    logger.info(
                        "owner_queue: %s approved — cron job %s re-armed to run on "
                        "the next tick and redeem the grant; no wake",
                        display_id, job_id)
                    verb = ("approved — the scheduled job re-runs on the next tick "
                            "and redeems the grant")
                else:
                    logger.info(
                        "owner_queue: %s approved — cron job %s not re-armed "
                        "(not scheduled or store unreadable); its next run redeems "
                        "the grant", display_id, job_id)
                    verb = ("approved — the scheduled job's next run redeems the grant")
            elif session_id:
                # No goal to re-arm (an interactive ask). The wake still helps for
                # a non-money action, but it must NOT promise a retry a money verb
                # would refuse on this turn — that promise is what produced a loop
                # of "approved, but blocked" reports.
                text = _approval_wake_text(tool_name, display_id)
                meta = {"kind": "approval_granted", "ask_id": display_id}
                delivered = False
                if _owns_session_locally(task_agent, session_id):
                    # THIS process owns the session (resident here) -> wake it
                    # in-process now. Fire-and-forget, fail-open.
                    deliver = getattr(task_agent, "deliver_self_wake", None)
                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None and callable(deliver):
                        loop.create_task(deliver(session_id, user_id, text,
                                                 metadata=meta))
                        delivered = True
                if not delivered:
                    # The deciding process does NOT own the session (prod: the
                    # console decides, the agent owns it). A durable wake row ->
                    # the OWNING process's wake-drain tick delivers it in-process
                    # (survives a restart of either service, no new port).
                    # `deliver_self_wake` refuses a remote session, so the wake
                    # MUST run where the session lives, not here.
                    _wake_queue_for_board(board).enqueue(
                        session_id, user_id, text, metadata=meta)
        except Exception:
            logger.debug("resume-on-grant wake skipped (fail-open)", exc_info=True)
    return True, f"tool-approval request {display_id} {verb}"


def _cron_job_id(session_id: str) -> Optional[str]:
    """The cron job this session is running, or None. Fail-open to None: an ask
    that cannot name its job is still created and still approvable — it merely
    redeems on the job's own next scheduled tick instead of right away."""
    try:
        from agents.task.goals.autonomy_marker import cron_job_for_session
        return cron_job_for_session(session_id) or None
    except Exception:
        logger.debug("owner_queue: cron-job-for-session lookup failed (fail-open)",
                     exc_info=True)
        return None


def _rearm_cron_job(board: Any, job_id: str) -> bool:
    """Pull *job_id*'s ``next_run_at`` to now so the next scheduler tick runs it
    and redeems the grant. ``cron.db`` lives beside the board's ``goals.db``
    (same data home, same rule ``_wake_queue_for_board`` uses). Returns True
    when the row was re-armed; False (logged) when it was not — the grant is
    still live for the job's own next scheduled run either way."""
    from datetime import datetime, timezone
    try:
        from core.cron_rearm import cron_db_beside, rearm_job
        cron_db = cron_db_beside(getattr(board, "db_path", None))
        if cron_db is None:
            return False
        return rearm_job(cron_db, job_id, datetime.now(timezone.utc))
    except Exception:
        logger.debug("owner_queue: cron re-arm failed for %s (fail-open)", job_id,
                     exc_info=True)
        return False


def _blocked_goal_ids(session_id: str) -> list:
    """``[goal_id]`` when this session is running one, else ``[]``.

    Fail-open to empty: a missing mapping must never stop an ask being created.
    An ask that exists but does not auto-re-arm is recoverable by hand
    (`polyrob goals retry`); an ask that was never created is the dead end 039
    exists to close.
    """
    try:
        from agents.task.goals.autonomy_marker import goal_for_session
        gid = goal_for_session(session_id)
        return [gid] if gid else []
    except Exception:
        logger.debug("owner_queue: goal-for-session lookup failed (fail-open)",
                     exc_info=True)
        return []


async def _push_owner_notification(container: Any, user_id: str, text: str) -> None:
    """Best-effort — reuses the SAME owner-resolution + delivery rail
    ``core.self_evolution.push_owner_message``/``cron/delivery.py`` ride
    (`core.surfaces.user_delivery.deliver_user_message`). Never raises."""
    if container is None or not text or not user_id:
        return
    try:
        from core.surfaces.user_delivery import deliver_user_message
        await deliver_user_message(container, user_id, text, source="approval")
    except Exception:
        logger.debug("owner_queue: notification push skipped (fail-open)", exc_info=True)


def _emit_payment_auto_approved(user_id: str, session_id: str, tool_name: str,
                                request_id: Optional[str], amount: Any,
                                purpose: Optional[str]) -> None:
    try:
        from core.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(
                "payment_auto_approved", user_id=user_id or "", session_id=session_id or "",
                source="payment_approval", attrs={
                    "tool_name": tool_name, "request_id": request_id,
                    "amount_usd": amount, "purpose": purpose,
                })
    except Exception:
        logger.debug("owner_queue: auto-approval audit event skipped", exc_info=True)


def _auto_approval_text(tool_name: str, request_id: Optional[str], amount: Any,
                        purpose: Optional[str]) -> str:
    text = f"Auto-approved payment request ({tool_name})"
    if request_id:
        text += f" {request_id}"
    if isinstance(amount, (int, float)):
        text += f": ${float(amount):.2f}"
    if purpose:
        text += f" — {purpose}"
    return text + " (within caps; PAYMENT_APPROVAL_MODE=auto)."


def make_payment_auto_notify_hook(container: Any, payment_tools: Iterable[str],
                                  taint_probe: Optional[Callable[[], bool]] = None,
                                  skip_fn: Optional[Callable[[str, Any], bool]] = None,
                                  audit_only: bool = False):
    """PAYMENT_APPROVAL_MODE=auto: a payment-creation action is NOT queued through
    `owner_queue` — this post-tool-call hook instead fires ONE owner notification +
    a first-class ``payment_auto_approved`` audit event for every WITHIN-CAP
    creation. The caps themselves live in `modules/x402/invoicing.py` and already
    reject over-cap (surfaced as ``result.error``) — this never re-checks them, it
    only reacts to what already succeeded.

    ``taint_probe`` (fix pass 1 / Finding 1): the SAME correspondent-taint
    short-circuit as :meth:`OwnerQueueApprover.request` — a truthy (or raising,
    fail-CLOSED) probe suppresses the notification + audit event entirely. This is
    defense-in-depth: `agents/task/agent/core/correspondent_gate.py`'s pre-tool-call
    hook already denies ``x402_request`` outright while tainted (so ``result.error``
    would already short-circuit the line above), but that hook is wired later, in
    agent construction — this keeps the auto-notify path honest even if the gate is
    ever unregistered/reordered.

    ``audit_only`` (039 Unit A): keep the ``payment_auto_approved`` event, drop the
    owner MESSAGE. The on-chain spend verbs now report through
    ``core/wallet/tx_notify.py``, which knows the chain, the amounts, the hash, the
    cap headroom and the settled outcome. This hook only ever knew the action name,
    and it said "Auto-approved payment request … (within caps;
    PAYMENT_APPROVAL_MODE=auto)" — which on 2026-09-12 the owner read about a
    bridge he had approved by hand twice. Two notices about one transaction, one of
    them wrong, is worse than one right one. The audit event is a real fact and
    stays.
    """
    tools_set = {t for t in (payment_tools or []) if t}

    async def _hook(action_name, params, result, context) -> None:
        if action_name not in tools_set:
            return
        if getattr(result, "error", None):
            return  # rejected by the tool's own caps — nothing to auto-approve
        if skip_fn is not None:
            # 023 D3: a call that moved no money is not worth an owner ping (a
            # dry-run swap succeeds loudly but broadcasts nothing). Fail-OPEN —
            # a broken predicate notifies, which is the noisy-but-honest side.
            try:
                if skip_fn(action_name, params or {}):
                    return
            except Exception:
                logger.debug("auto_notify: skip predicate raised — notifying anyway",
                             exc_info=True)
        if taint_probe is not None:
            try:
                tainted = bool(taint_probe())
            except Exception:
                logger.debug(
                    "owner_queue: auto-notify taint probe raised — treating as "
                    "tainted (fail-closed, no notify)", exc_info=True,
                )
                tainted = True
            if tainted:
                logger.info(
                    "owner_queue: correspondent-tainted turn — suppressing "
                    "auto-approval notify for '%s'", action_name,
                )
                return
        user_id = getattr(context, "user_id", None) or ""
        session_id = getattr(context, "session_id", None) or ""
        meta = getattr(result, "metadata", None) or {}
        request_id = meta.get("request_id")
        amount = meta.get("amount_usd")
        purpose = meta.get("purpose")
        _emit_payment_auto_approved(user_id, session_id, action_name, request_id, amount, purpose)
        if audit_only:
            return
        await _push_owner_notification(
            container, user_id,
            _auto_approval_text(action_name, request_id, amount, purpose))

    return _hook


def _emit_tool_auto_approved(user_id: str, session_id: str, action_name: str) -> None:
    """Durable ``tool_auto_approved`` audit event for an act-and-report execution
    (013 T4; mirrors :func:`_emit_payment_auto_approved`). Fail-open."""
    try:
        from core.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(
                "tool_auto_approved", user_id=user_id or "", session_id=session_id or "",
                source="approval", attrs={"action": action_name})
    except Exception:
        logger.debug("auto_notify: audit event skipped", exc_info=True)


def make_tool_auto_notify_hook(container: Any, tools: Iterable[str],
                               taint_probe: Optional[Callable[[], bool]] = None):
    """013 T4 — act-and-report under AUTONOMY_MODE=autonomous: a gated action in
    the *reported* lane (see ``tools/controller/approval.py::
    autonomous_gating_lanes``) is allowed by the ``auto_notify`` pre-hook; THIS
    post-tool-call hook then fires ONE owner notification + a first-class
    ``tool_auto_approved`` audit event for every successful (non-error) run —
    reckless-but-observable, never silent. Mirrors
    :func:`make_payment_auto_notify_hook` (same result/error short-circuit, same
    fail-CLOSED ``taint_probe`` suppression, same ``_push_owner_notification``
    delivery rail).
    """
    tools_set = {t for t in (tools or []) if t}

    async def _hook(action_name, params, result, context) -> None:
        if action_name not in tools_set:
            return
        if getattr(result, "error", None):
            return  # the action failed/was refused — nothing was auto-approved
        if taint_probe is not None:
            try:
                tainted = bool(taint_probe())
            except Exception:
                logger.debug(
                    "auto_notify: taint probe raised — treating as tainted "
                    "(fail-closed, no notify)", exc_info=True,
                )
                tainted = True
            if tainted:
                logger.info(
                    "auto_notify: correspondent-tainted turn — suppressing "
                    "act-and-report notify for '%s'", action_name,
                )
                return
        user_id = getattr(context, "user_id", None) or ""
        session_id = getattr(context, "session_id", None) or ""
        _emit_tool_auto_approved(user_id, session_id, action_name)
        await _push_owner_notification(
            container, user_id,
            f"[auto-approved] {action_name} ran under AUTONOMY_MODE=autonomous")

    return _hook


class OwnerQueueApprover(ApprovalProvider):
    """Durable, remote-capable owner approval queue (Task 9 / G-2).

    Every `request()` call for the SAME ``(tool_name, params, tenant)`` resolves to
    the same durable ask row (via the stable request hash), so a caller retrying
    after a timeout re-polls the SAME ask instead of spamming a new one/a new
    notification.
    """

    def __init__(self, *, user_id: Optional[str] = None, home_dir: Any = None,
                 container: Any = None, poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
                 board: Any = None, taint_probe: Optional[Callable[[], bool]] = None):
        self._default_user_id = user_id
        self._home_dir = home_dir
        self._container = container
        self._poll_interval = max(0.05, float(poll_interval))
        self._board_override = board
        # fix pass 1 (Finding 1): correspondent-taint short-circuit, mirrored from
        # the SAME orchestrator flag `correspondent_gate` reads
        # (`_orch._correspondent_tainted`). Optional constructor kwarg (natural for
        # tests / direct construction) PLUS `set_taint_probe` below (the generic
        # `ApprovalProvider` factory in `tools/controller/approval.py` only threads
        # `user_id`/`home_dir` through — Controller.__init__ injects this
        # post-construction since it's the site with an `orchestrator` reference).
        self._taint_probe = taint_probe
        # Test/observability seam: incremented at poll start, decremented in the
        # `finally` — proves the cancellation-safety contract (no dangling poll).
        self._active_polls = 0

    def set_taint_probe(self, probe: Optional[Callable[[], bool]]) -> None:
        """Post-construction injection seam for ``taint_probe`` (see ``__init__``)."""
        self._taint_probe = probe

    # -- collaborators ----------------------------------------------------------

    def _resolve_container(self) -> Any:
        if self._container is not None:
            return self._container
        try:
            from core.container import DependencyContainer
            return DependencyContainer.get_instance()
        except Exception:
            return None

    def _board(self):
        if self._board_override is not None:
            return self._board_override
        from agents.task.goals.board import GoalBoard
        return GoalBoard(_goals_db_path(self._home_dir))

    @staticmethod
    def _grant_ttl_hours() -> float:
        from core.config_policy import approval_grant_ttl_hours
        return approval_grant_ttl_hours()

    # -- grant + ask lookups ------------------------------------------------------

    def _consume_grant(self, board: Any, user_id: str, req_hash: str) -> bool:
        """Atomically consume an unexpired one-shot grant left by a post-timeout
        owner decision on an identical prior request. Returns True at most once
        per grant — a second identical call after consumption falls through to a
        fresh ask (`consume_ask_grant` is the atomic single-winner claim)."""
        from agents.task.goals.board import ASK_FULFILLED
        ttl_hours = self._grant_ttl_hours()
        now = time.time()
        for a in board.asks(user_id=user_id, status=ASK_FULFILLED):
            payload = a.payload or {}
            if payload.get("ask_kind") != TOOL_APPROVAL_ASK_KIND:
                continue
            if payload.get("request_hash") != req_hash:
                continue
            if payload.get("grant_consumed"):
                continue
            if a.completed_at is None or (now - a.completed_at) > ttl_hours * 3600:
                continue
            if board.consume_ask_grant(a.id):
                return True
        return False

    @staticmethod
    def _find_open_ask(board: Any, user_id: str, req_hash: str):
        from agents.task.goals.board import ASK_OPEN
        for a in board.asks(user_id=user_id, status=ASK_OPEN):
            payload = a.payload or {}
            if payload.get("ask_kind") == TOOL_APPROVAL_ASK_KIND \
                    and payload.get("request_hash") == req_hash:
                return a
        return None

    # -- ApprovalProvider ---------------------------------------------------------

    async def request(self, action_name: str, params: Dict[str, Any], context: Any,
                      *, hash_params: Optional[Dict[str, Any]] = None) -> bool:
        """Ask the owner. ``params`` is what he SEES; ``hash_params`` is what the
        grant is keyed on, when the two must differ.

        They must differ whenever the displayed figures are re-derived per attempt.
        A bridge re-quotes before every try, so `min_out`/`usd`/`request_id` move
        each time — and keying the grant on them meant every attempt minted a NEW
        tap (the owner answered three for one bridge on 2026-09-12) and no
        approval could ever be redeemed by a later run, because the later run's
        hash did not match the one he approved.

        The key is therefore the stable INTENT. The price is not left unguarded by
        that: the arrival floor, the simulation, the per-transaction ceiling, the
        rolling daily cap and `tx_guard` all re-assert against the FRESH quote at
        execution, and the grant itself expires (``approval_grant_ttl_hours``).
        """
        user_id = getattr(context, "user_id", None) or self._default_user_id or ""
        from core.wallet.authority import money_action, owner_refusal
        if money_action(action_name) and owner_refusal(user_id):
            return False
        session_id = getattr(context, "session_id", None) or ""

        # Defense in depth: a forged/leaf/sub-agent/autonomous-reentry turn never
        # earns a queued owner ask — reuses the SAME SSOT the writable-skills/
        # message-tool/self_context gates already check (no parallel detector).
        # MH1: fail CLOSED, mirroring the correspondent-taint probe below — a probe
        # that raises must DENY (we can't prove the turn is a genuine owner turn),
        # not fail-open into creating a durable ask + owner notification + a 300s
        # poll block for what may be a forged/autonomous turn.
        try:
            from tools.controller.action_registration import (
                _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
            forged = _is_forged_or_autonomous_turn(context, None)
            # 039: ONE forged-shaped origin may ASK — a goal/cron-dispatched run on
            # the MAIN agent. Live dead end found on prod 2026-09-12: the bridge
            # runs on caps-not-taps, so an autonomous run reaches it, and above
            # DEFI_AUTONOMOUS_MAX_USD it escalates to HERE — which denied it
            # outright, created no ask, and never reached `_consume_grant`. So an
            # above-ceiling autonomous spend was unapprovable by ANY route: no tap
            # to press, and an approval given earlier could not be redeemed later
            # either. Holding no tap id, the agent told its owner to
            # `/approve <relay-request-id>` — a handle that does not exist.
            #
            # This grants NO new authority. The run still cannot self-approve; it
            # may only ASK, and redeem what the owner already granted. The detector
            # is the strict one (`turn_origin._is_autonomous_goal_turn`): main
            # agent, orchestrator role, not a sub-agent, not a self-wake or
            # delegation-result re-entry, live autonomy marker, fail-closed on any
            # raise. Leaf, self-wake and tainted turns are untouched below.
            goal_turn = bool(forged and _is_autonomous_goal_turn(context, None))
        except Exception:
            logger.debug(
                "owner_queue: forged-turn probe raised — treating as forged "
                "(fail-closed, deny, no ask)", exc_info=True)
            forged, goal_turn = True, False
        if forged and not goal_turn:
            logger.info(
                "owner_queue: forged/leaf/autonomous turn denied for '%s' "
                "(no ask created)", action_name,
            )
            from core.security.refusals import record_refusal
            record_refusal("approval_denied", tool=action_name, user_id=user_id,
                           detail="owner_queue: no ask")
            return False

        # fix pass 1 (Finding 1): defense in depth — a correspondent-TAINTED turn
        # must never earn a queued owner ask/notification either. Without this, a
        # forged correspondent reply that gets the LLM to attempt a payment tool
        # would create a durable ask + push a real owner notification + block up to
        # `payment_approval_timeout_sec()` (default 300s), all BEFORE
        # `agents/task/agent/core/correspondent_gate.py`'s pre-tool-call hook (wired
        # later, in agent construction — after this provider is registered in
        # Controller.__init__) gets a chance to unconditionally deny execution. No
        # money moves either way, but it's an unbounded owner-notification-spam /
        # alert-fatigue vector otherwise. Fail-CLOSED: a raising probe is treated as
        # tainted — we can't prove the turn is clean.
        if self._taint_probe is not None:
            try:
                tainted = bool(self._taint_probe())
            except Exception:
                logger.debug(
                    "owner_queue: taint probe raised — treating as tainted "
                    "(fail-closed, deny, no ask)", exc_info=True,
                )
                tainted = True
            if tainted:
                logger.info(
                    "owner_queue: correspondent-tainted turn denied for '%s' "
                    "(no ask created)", action_name,
                )
                return False

        try:
            board = self._board()
        except Exception:
            logger.error("owner_queue: asks store unavailable — denying '%s'",
                         action_name, exc_info=True)
            return False

        norm_params = _normalize_params(params)
        req_hash = compute_request_hash(
            action_name, _normalize_params(hash_params) if hash_params else norm_params,
            user_id)

        if self._consume_grant(board, user_id, req_hash):
            logger.info("owner_queue: one-shot grant consumed for %s (hash=%s)",
                       action_name, req_hash)
            # H4: notify on consumption — the post-timeout redemption is otherwise
            # a silent execution (owner approved earlier, then walked away). One
            # approval still authorizes exactly ONE execution (the CAS in
            # `_consume_grant` is single-winner), but the owner should see it fire.
            await _push_owner_notification(
                self._resolve_container(), user_id,
                f"✅ Approved & executed: {action_name} [{req_hash[:10]}]")
            return True

        ask = self._find_open_ask(board, user_id, req_hash)
        created_new = ask is None
        if ask is not None:
            # REUSING an open ask: widen the goal list rather than leaving it as
            # first created. Without this, an approval raised again by a later
            # goal run re-arms whatever the FIRST run happened to stamp — which,
            # for every ask created before 039, is nothing at all.
            board.add_ask_blocked_goals(ask.id, _blocked_goal_ids(session_id))
        if ask is None:
            summary = _params_summary(norm_params)
            ask = board.create_ask(
                user_id=user_id,
                what=f"Approve {action_name}? [{req_hash[:10]}]",
                why=f"tool={action_name} params={summary} session={session_id}",
                extra_payload={
                    "ask_kind": TOOL_APPROVAL_ASK_KIND,
                    "tool_name": action_name,
                    "params_summary": summary,
                    "request_hash": req_hash,
                    "session_id": session_id,
                    "grant_consumed": False,
                    # 2026-09-18: the cron twin of blocks_goal_ids — an approval
                    # re-arms THIS job to run on the next tick (see
                    # decide_tool_approval). None for every other origin.
                    "cron_job_id": _cron_job_id(session_id),
                },
                # 039: name the goal this ask blocks, so `decide_ask`'s EXISTING
                # unblock hop re-arms it on approval. Without it the owner
                # presses /approve and nothing happens — the grant sits
                # unredeemed and the goal stays blocked, which is the same
                # "owner intent does not stick" failure one layer down. Empty
                # for an interactive turn and for a cron/planner run with no
                # goal row; both are real answers, not failures.
                blocks_goal_ids=_blocked_goal_ids(session_id),
                force=True,  # exact-hash dedup above already did the real work
            )
        if created_new:
            # 030 WS-E1: a human grant card (amount/target/purpose + deadline +
            # the one-shot-grant explainer), never a raw JSON dump.
            try:
                from tools.controller.approval import approval_wait_timeout_sec
                from tools.controller.grant_card import render_grant_card
                _ttl = None
                try:
                    from core.config_policy import approval_grant_ttl_hours
                    _ttl = approval_grant_ttl_hours()
                except Exception:
                    pass
                card = render_grant_card(
                    action_name, norm_params, tap_display_id(ask.id),
                    timeout_sec=approval_wait_timeout_sec("owner_queue"),
                    grant_ttl_hours=_ttl)
            except Exception:
                card = (f"🔐 Approval needed: {action_name}\n"
                        f"Reply /approve {tap_display_id(ask.id)} or "
                        f"/reject {tap_display_id(ask.id)}")
            await _push_owner_notification(self._resolve_container(), user_id, card)

        if goal_turn:
            # Return rather than poll. A goal run that sits on a dispatcher slot
            # for the whole timeout starves every other goal, and the wait buys
            # nothing: the ask is durable and the grant outlives this run, so the
            # owner answers whenever and the next dispatch redeems it above.
            logger.info(
                "owner_queue: autonomous goal run asked for '%s' and released the "
                "slot (ask %s); the next run redeems the grant", action_name, ask.id)
            return False

        self._active_polls += 1
        try:
            while True:
                row = board.get(ask.id)
                if row is None:
                    return False
                if row.status != "open":
                    approved = (row.payload or {}).get("decision") == "approved"
                    if not approved:
                        return False  # rejected — no grant to consume
                    # H4: an IN-BAND decision (owner approved while we were still
                    # polling) must CONSUME the one-shot grant right here — exactly
                    # as the post-timeout redemption path (`_consume_grant`) does.
                    # Otherwise the ask is left fulfilled + grant_consumed=false and
                    # a later byte-identical request within APPROVAL_GRANT_TTL_HOURS
                    # redeems the SAME approval for a SECOND execution (one approval
                    # -> two executions). The CAS is the SAME single-winner claim, so
                    # this in-band consume and a concurrent grant-redeem can never
                    # BOTH win: we authorize this execution ONLY if we win the CAS.
                    try:
                        consumed = board.consume_ask_grant(ask.id)
                    except Exception:
                        # Fail CLOSED on a store error: we cannot prove the
                        # one-approval-one-execution invariant, so deny this leg.
                        # The unconsumed grant is still redeemable exactly once by a
                        # later identical request (the documented one-shot path), so
                        # a genuine owner approval is not lost.
                        logger.debug(
                            "owner_queue: in-band grant consume raised — denying "
                            "(fail-closed) for %s (hash=%s)", action_name, req_hash,
                            exc_info=True)
                        return False
                    if consumed:
                        logger.info(
                            "owner_queue: in-band approval consumed one-shot grant "
                            "for %s (hash=%s)", action_name, req_hash)
                        await _push_owner_notification(
                            self._resolve_container(), user_id,
                            f"✅ Approved & executed: {action_name} "
                            f"[{req_hash[:10]}]")
                        return True
                    # Lost the CAS: a concurrent identical request already redeemed
                    # this single grant and is the one authorized execution — never
                    # double-execute off the same approval.
                    logger.info(
                        "owner_queue: in-band grant already consumed elsewhere for "
                        "%s (hash=%s) — denying duplicate", action_name, req_hash)
                    return False
                await asyncio.sleep(self._poll_interval)
        finally:
            self._active_polls -= 1


register_approval_provider("owner_queue", OwnerQueueApprover)
