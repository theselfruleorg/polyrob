"""tx_guard steps 0-2b: who is asking, the pauses, the turn origin and the token-bound account.

Part of the money authorizer split (W6): every function here is one step of
``core.wallet.tx_guard._authorize``, moved VERBATIM. A step reads what it needs
from the :class:`~core.wallet.tx_guard_steps.context.GuardContext`, returns a
refusing :class:`~core.wallet.tx_guard.Decision` to stop the guard, or writes
what later steps read back to the context and returns ``None``. Exceptions are
not caught here: a step raises exactly where the single function raised.

⚠️ Module-level helpers are reached as ``_tg.<name>`` on purpose — callers and
tests monkeypatch them on ``core.wallet.tx_guard``, and a direct import would
bind the original. The step ORDER lives in ``tx_guard_steps.STEPS`` only.
"""
from typing import Optional

from core.wallet import tx_guard as _tg
from core.wallet.tx_guard import Decision
from core.wallet.tx_guard_steps.context import GuardContext


def step_0_owner_authority(c: GuardContext) -> Optional[Decision]:
    """0. Who is asking? (owner-direct, owner-granted, the pause probe)."""
    intent = c.intent
    execution_context = c.execution_context
    tool_self = c.tool_self
    halted_fn = c.halted_fn
    forged_fn = c.forged_fn
    # -- 0. Who is asking? -------------------------------------------------
    # Owner intent in a genuine owner turn IS authorization: the pause (steps
    # 1/1b and the ledger's copy) and the autonomous ceiling (step 9) bound what
    # the agent does ON ITS OWN, not the owner acting through it. The hard bounds
    # (principal, simulation, declared bound, per-tx + daily caps) bind everyone.
    # 2026-09-26: the owner could not send his own ETH for three hours — "Stop"
    # paused everything including his seat, then the $300 ceiling returned a lane
    # nothing consumed. ⚠️ A missing context is never the owner (the signer).
    owner_direct, owner_granted = _tg.owner_authority(
        execution_context, forged_fn, tool_self, intent)
    pause_fn = (lambda: False) if owner_direct else halted_fn
    c.owner_direct = owner_direct
    c.owner_granted = owner_granted
    c.pause_fn = pause_fn
    return None


def step_1_owner_pause(c: GuardContext) -> Optional[Decision]:
    """1. Owner pause (fail closed)."""
    intent = c.intent
    execution_context = c.execution_context
    tool_self = c.tool_self
    pause_fn = c.pause_fn
    # -- 1. Owner pause (fail closed) --------------------------------------
    # ⚠️ NOT a kill-switch. `halted_fn` is `AutonomyConfig.autonomy_halted()`, which
    # is literally `not allows("dispatch").allowed` — a FACET of the 031 pause
    # record. The legacy text here said "autonomy is HALTED (owner kill-switch)",
    # which named a lever that does not exist and offered no remedy at all.
    # `125f83dd` fixed that wording for the bridge only; every OTHER money verb
    # (swap, solana_swap, transfer, approve_token, revoke_approval) still routed
    # through this branch and still sent the owner hunting for a phantom switch
    # (census, 2026-09-12). One shared renderer now serves both.
    try:
        if pause_fn():
            from core.autonomy_control import pause_refusal_text
            # 045: a money verb refused by the owner's own pause is exactly the
            # kind of unauthorized-action the lane-2 ledger exists to count, and
            # `pause` is already a declared REFUSAL_REASONS slug. It was silent.
            # ⚠️ getattr-with-default, never `intent.chain`: this call sits
            # INSIDE the pause try/except, so an attribute error here would be
            # swallowed as "pause probe failed" — the recorder would have
            # changed the guard's own refusal text. A ledger write must never
            # be able to alter an OUTCOME.
            _tg._record_money_refusal("pause", tool_self, execution_context,
                                      detail=f"chain={getattr(intent, 'chain', '?')}")
            # force=True: `halted_fn` may be the legacy env/touch-file facet or an
            # injected probe, so it can read STOPPED while the record reads allowed.
            # The refusal must carry the same honesty either way.
            return Decision(False, pause_refusal_text(
                "dispatch", what="this transaction", force=True))
    except Exception as exc:
        return Decision(False, f"refused: pause probe failed ({exc}); failing closed")
    return None


def step_1b_entry_pause(c: GuardContext) -> Optional[Decision]:
    """1b. Owner entry-pause (fail closed; exits always pass)."""
    intent = c.intent
    execution_context = c.execution_context
    tool_self = c.tool_self
    entry_paused_fn = c.entry_paused_fn
    owner_direct = c.owner_direct
    # -- 1b. Owner entry-pause (fail closed; exits always pass) ------------
    # 2026-08-28: give the entry-pause a structural home instead of leaving
    # it to each goal's own prose (one stream-manifest goal traded through it
    # unnoticed). Exit-shaped intents (sells to quote, revokes) are never
    # blocked — the pause is about not adding NEW risk.
    try:
        # A collection reveal opens no position and moves nothing (the full pause above
        # still holds it); holding it here would only let a due id re-commit
        # (the collection's rule: never delay a due id).
        if (not owner_direct and not intent.is_collection_reveal and entry_paused_fn()
                and not _tg._intent_is_exit_shaped(intent)):
            _tg._record_money_refusal(
                    "pause", tool_self, execution_context,
                    detail=f"entry_pause chain={getattr(intent, 'chain', '?')}")
            return Decision(False, (
                "refused: new treasury entries are PAUSED (owner entry-pause) "
                "— exits still run; the owner lifts it with /resume trading"))
    except Exception as exc:
        return Decision(False, f"refused: entry-pause probe failed ({exc}); failing closed")
    return None


def step_2_turn_origin(c: GuardContext) -> Optional[Decision]:
    """2. Turn origin, then the principal."""
    intent = c.intent
    execution_context = c.execution_context
    tool_self = c.tool_self
    forged_fn = c.forged_fn
    autonomous_ok_fn = c.autonomous_ok_fn
    # -- 2. Turn origin ----------------------------------------------------
    # W5: the turn-origin bar is the money kernel's (``authorize_spend`` step 3,
    # the same detector texts). What stays here is the guard's own lane: the
    # autonomous admission needs the TRADING flag too (``_autonomous_turn_allowed``)
    # and a refused forged turn may still EXIT under DEFI_MONITOR_EXITS.
    from core.money.authorize import FORGED_TEXTS, SpendIntent, authorize_spend
    autonomous_origin = False
    monitor_exit = False
    # 086 W2: a run that belongs to a trading mandate (``metadata["mandate"]``) has
    # its ENTRIES checked by the kernel's mandate step: an intent that receives a
    # token and is not exit-shaped. Exits, sends and approvals are never gated.
    mandate_entry = bool(intent.inflow_token) and not _tg._intent_is_exit_shaped(intent)
    # 099 W2: an ORDER run (``metadata["mandate"]`` = ``ord_…``) hands the kernel's offer
    # step what this transaction does: where value goes and which contract it calls.
    _meta = getattr(execution_context, "metadata", None)
    _order = str((_meta or {}).get("mandate") or "") if isinstance(_meta, dict) else ""
    offer_scope = None
    if _order.startswith("ord_"):
        from core.money.hooks import offer_recipient
        c.offer_recipient = offer_recipient(_order)
        if getattr(intent, "is_resource_call", False) and \
                str(getattr(intent, "resource_recipient", None) or "").lower() != \
                str(c.offer_recipient or "").lower():
            return Decision(False, "refused: the resource call names a recipient that is not "
                                   "the order's recipient")
        if getattr(intent, "is_deploy", False) or getattr(intent, "via_account", None):
            return Decision(False, "refused: an order run deploys nothing and acts through no "
                                   "account; it calls its offer's resources or pays its recipient")
        _dest = [] if getattr(intent, "is_resource_call", False) else [intent.to] + [
            g[1] for g in (getattr(intent, "expected_allowance_grants", None) or ())]
        offer_scope = {"chain": str(intent.chain or "").lower(), "destinations": _dest,
                       "is_resource_call": bool(getattr(intent, "is_resource_call", False)),
                       "contract": str(c.tx.get("to") or "").lower(),
                       "selector": str(c.tx.get("data") or c.tx.get("input") or "")[:10].lower()}
    elif getattr(intent, "is_resource_call", False):
        if execution_context is not None:
            return Decision(False, "refused: a resource call is only for a run that fulfils "
                                   "an order")
        # polyrob-signer's re-check (no execution context, no order store): the agent's
        # guard already bound the order (step 4c); the receipt shape below still runs
        # against the recipient that guard checked.
        c.offer_recipient = getattr(intent, "resource_recipient", None)
    c.offer_scope = offer_scope
    verdict = authorize_spend(
        SpendIntent.turn_origin(mandate=mandate_entry, entry=mandate_entry,
                                chain=str(intent.chain or ""), token=intent.inflow_token,
                                usd=intent.max_spend_usd, offer_scope=offer_scope,
                                idempotency_key=getattr(intent, "idempotency_key", None),
                                # An order run's budget is only CHECKED here; it is booked
                                # after the final allow (reserve_offer_budget).
                                dry_run=offer_scope is not None),
        execution_context, forged_fn=forged_fn,
        autonomous_ok_fn=lambda ctx, ts: _tg._autonomous_turn_allowed(
            ctx, ts, autonomous_ok_fn, offer_scoped=offer_scope is not None),
        tool_self=tool_self)
    if verdict.refused and verdict.step == "mandate":
        _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                  detail=f"mandate: {verdict.reason}")
        return Decision(False, verdict.reason)
    if verdict.refused and verdict.reason not in FORGED_TEXTS:
        # No detector, or the detector raised: the kernel's sentence, fail closed.
        return Decision(False, verdict.reason)
    if verdict.refused:
        try:
            # 2026-08-26 exit untying: a forged MAIN-agent turn — the self-wake
            # monitor loop — may still CLOSE a position when the operator armed
            # DEFI_MONITOR_EXITS. Stop rules fire on monitor ticks; refusing the
            # exit here left fired stops unexecutable until the next goal run.
            # Exit-shaped only, main agent only; a sell additionally has its
            # measured quote inflow asserted after simulation.
            # An ORDER run is admitted only through the offer step (099): it never
            # rides the monitor-exit carve-out past step 4c.
            if (_tg.monitor_exits_enabled() and offer_scope is None
                        and not getattr(execution_context, "is_sub_agent", False)
                        and getattr(execution_context, "role", "leaf") == "orchestrator"
                        and _tg._intent_is_exit_shaped(intent)):
                monitor_exit = True
                _tg.logger.info(
                        "tx_guard.monitor_exit chain=%s token=%s — forged turn "
                        "allowed for an EXIT-shaped operation (DEFI_MONITOR_EXITS)",
                        intent.chain, intent.token)
            else:
                from core.security.refusals import record_refusal
                record_refusal(
                    "forged_turn",
                    tool=getattr(tool_self, "name", None) or "",
                    user_id=getattr(execution_context, "user_id", None) or "",
                    kind=("money:forged_text" if verdict.reason == FORGED_TEXTS[0]
                          else "money:autonomous_unarmed"))
                return Decision(False, verdict.reason)
        except Exception as exc:
            return Decision(False, f"refused: could not prove the turn is genuine ({exc})")
    # A genuine owner turn is not forged; only the unattended goal-dispatched
    # lane (or the monitor-exit lane above) reaches here forged-but-allowed.
    # Remember it so step 9 can demand an aggregate damage bound (a daily cap)
    # that an owner-driven trade doesn't need.
    autonomous_origin = bool(verdict.autonomous or monitor_exit)

    principal_error = _tg._principal_refusal(execution_context)
    if principal_error:
        return Decision(False, f"refused: {principal_error}")
    c.autonomous_origin = autonomous_origin
    c.monitor_exit = monitor_exit
    return None


def step_2b_token_bound_account(c: GuardContext) -> Optional[Decision]:
    """2b. Token-bound account: the key signs, the account holds."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    # -- 2b. Token-bound account: the key signs, the account holds (050 §7.3) --------------
    # 069 v4: the signing treasury OWNS the NFT (checked in the pre-flight) — no grants.
    # ⚠️ From here on `tx` is the INNER call when `via_account` is set, so every
    # structural destination check below judges what the ACCOUNT will call;
    # `outer_tx` (what the treasury key signs) is what gets simulated and what
    # pays the fee. `holder` becomes the account; `sender` is the signer.
    outer_tx = tx
    sender = None
    journal_to = None
    if intent.is_journal_entry and not intent.via_account:
        return Decision(False, ("refused: a journal entry is written THROUGH a token-bound "
                                "account — declare `via_account`"))
    if intent.via_account_journal and not intent.via_account:
        return Decision(False, ("refused: a journal leg rides a token-bound account call — "
                                "declare `via_account`"))
    if intent.via_account:
        if intent.via_account_journal:
            tx, journal_to, via_error = _tg._strip_journal_leg(intent, tx, signer=holder)
            if via_error:
                _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                          detail=f"account: {via_error}")
                return Decision(False, f"refused: {via_error}")
        inner, via_error = _tg._via_account_inner(intent, tx)
        if not via_error and intent.is_journal_entry:
            journal_to, via_error = _tg._journal_only_refusal(intent, inner, signer=holder)
        if via_error:
            _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {via_error}")
            return Decision(False, f"refused: {via_error}")
        sender, holder, tx = holder, intent.via_account, inner
    elif intent.via_account_batch:
        return Decision(False, ("refused: an approve-spend-reset batch runs through a token-bound "
                                "account — declare `via_account`"))
    else:
        admin = _tg._account_call_refusal(tx, deep=not intent.is_deploy)
        if admin:
            _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {admin}")
            return Decision(False, f"refused: {admin}")
    c.tx = tx
    c.holder = holder
    c.journal_to = journal_to
    c.outer_tx = outer_tx
    c.sender = sender
    return None


def reserve_offer_budget(c) -> Optional[Decision]:
    """After the final allow: book an order run's spend against its order budget (099).

    Step 2 checked the budget without booking it. Booking only an ALLOWED transaction
    means a dry run's refusal, a reverted simulation or a cap refusal books nothing; an
    allowed dry run books what its real send then reuses (the same idempotency key).
    The same owner-direct exception as the kernel's offer step. Fails closed."""
    if c.offer_scope is None:
        return None
    from core.money.authority import owner_direct_turn
    ctx = c.execution_context
    if owner_direct_turn(ctx, c.forged_fn, c.tool_self):
        return None
    meta = getattr(ctx, "metadata", None)
    order = str((meta or {}).get("mandate") or "") if isinstance(meta, dict) else ""
    try:
        from core.money.hooks import offer_gate
        gate = offer_gate()
        if gate is None:
            return Decision(False, "refused: this run fulfils an order and no offer gate is "
                                   "registered; failing closed")
        refusal = gate(order, user_id=str(getattr(ctx, "user_id", "") or ""),
                       scope=c.offer_scope, usd=c.intent.max_spend_usd, dry_run=False,
                       key=getattr(c.intent, "idempotency_key", None),
                       session_id=getattr(ctx, "session_id", None))
    except Exception as exc:  # noqa: BLE001
        return Decision(False, f"refused: the order budget could not be booked ({exc}); "
                               f"failing closed")
    if refusal:
        _tg._record_money_refusal("money_gate", c.tool_self, ctx, detail=f"offer: {refusal}")
        return Decision(False, refusal)
    return None

