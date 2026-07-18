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
        preview = (a.body or a.title or "")[:160]
        out.append({
            "kind": TOOL_APPROVAL_ASK_KIND,
            "id": tap_display_id(a.id),
            "chars": len(preview),
            "preview": preview,
        })
    return out


def decide_tool_approval(board: Any, display_id: str, *, user_id: str,
                         approved: bool) -> tuple:
    """Resolve a (possibly ``tap-``-prefixed) ask id back to the real ask id and
    record the owner's decision. Returns ``(ok, message)`` — the shared handler
    behind Telegram `/approve` `/reject` and `polyrob owner promote/reject
    tool_approval`."""
    real_id = strip_tap_prefix(display_id) or display_id
    ok, _ = board.decide_ask(real_id, user_id=user_id, approved=approved)
    if not ok:
        return False, f"no open tool-approval request '{display_id}'"
    verb = "approved" if approved else "rejected"
    return True, f"tool-approval request {display_id} {verb}"


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
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
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
                                  taint_probe: Optional[Callable[[], bool]] = None):
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
    """
    tools_set = {t for t in (payment_tools or []) if t}

    async def _hook(action_name, params, result, context) -> None:
        if action_name not in tools_set:
            return
        if getattr(result, "error", None):
            return  # rejected by the tool's own caps — nothing to auto-approve
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
        await _push_owner_notification(
            container, user_id,
            _auto_approval_text(action_name, request_id, amount, purpose))

    return _hook


def _emit_tool_auto_approved(user_id: str, session_id: str, action_name: str) -> None:
    """Durable ``tool_auto_approved`` audit event for an act-and-report execution
    (013 T4; mirrors :func:`_emit_payment_auto_approved`). Fail-open."""
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
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

    async def request(self, action_name: str, params: Dict[str, Any], context: Any) -> bool:
        user_id = getattr(context, "user_id", None) or self._default_user_id or ""
        session_id = getattr(context, "session_id", None) or ""

        # Defense in depth: a forged/leaf/sub-agent/autonomous-reentry turn never
        # earns a queued owner ask — reuses the SAME SSOT the writable-skills/
        # message-tool/self_context gates already check (no parallel detector).
        # MH1: fail CLOSED, mirroring the correspondent-taint probe below — a probe
        # that raises must DENY (we can't prove the turn is a genuine owner turn),
        # not fail-open into creating a durable ask + owner notification + a 300s
        # poll block for what may be a forged/autonomous turn.
        try:
            from tools.controller.action_registration import _is_forged_or_autonomous_turn
            forged = _is_forged_or_autonomous_turn(context, None)
        except Exception:
            logger.debug(
                "owner_queue: forged-turn probe raised — treating as forged "
                "(fail-closed, deny, no ask)", exc_info=True)
            forged = True
        if forged:
            logger.info(
                "owner_queue: forged/leaf/autonomous turn denied for '%s' "
                "(no ask created)", action_name,
            )
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
        req_hash = compute_request_hash(action_name, norm_params, user_id)

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
                },
                force=True,  # exact-hash dedup above already did the real work
            )
        if created_new:
            await _push_owner_notification(
                self._resolve_container(), user_id,
                f"🔐 Approval needed: {action_name}\n{_params_summary(norm_params)}\n"
                f"Reply /approve {tap_display_id(ask.id)} or /reject {tap_display_id(ask.id)}",
            )

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
