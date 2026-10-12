"""tx_guard steps 5-6: simulate, then assert every measured delta against the declaration.

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


def step_5_simulate(c: GuardContext) -> Optional[Decision]:
    """5. Simulate."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    simulate_fn = c.simulate_fn
    journal_to = c.journal_to
    outer_tx = c.outer_tx
    sender = c.sender
    deltas = c.deltas
    # -- 5. Simulate -------------------------------------------------------
    spenders = sorted({s for (_t, s, _a) in intent.expected_allowance_grants}
                      | set(intent.watch_spenders)
                      | ({str(intent.to)} if intent.via_account_batch and intent.to else set()))
    tokens = [t for t in (intent.token, intent.inflow_token) if t]
    if intent.is_liquidity_op:
        tokens = sorted({t.lower() for t, _ in (*intent.lp_outflows, *intent.lp_inflows) if t})
    try:
        if sender:
            deltas = simulate_fn(tx=outer_tx, holder=holder, chain=intent.chain,
                                 tokens=tokens, spenders=spenders, sender=sender)
        else:
            deltas = simulate_fn(tx=tx, holder=holder, chain=intent.chain,
                                 tokens=tokens, spenders=spenders)
    except Exception as exc:
        return Decision(False, f"refused: simulation raised ({exc})")
    if not deltas.ok:
        return Decision(False, f"refused: simulation not trustworthy — {deltas.error}")

    if intent.is_journal_entry:
        from core.wallet import journal_log as _jl
        _moved = (deltas.native_delta or any(deltas.token_deltas.values())
                  or deltas.holder_transfers or deltas.holder_approvals or deltas.holder_nft_out
                  or deltas.holder_nft_in or deltas.holder_operator_grants
                  or deltas.holder_nft_approvals or deltas.holder_permit2_grants
                  or getattr(deltas, "holder_6909_approvals", ()))
        if _moved:
            return Decision(False, "refused: a journal entry moved something of the account")
        if (str(journal_to), _jl.TOPIC_ENTRY) not in set(deltas.event_topics or ()):
            return Decision(False, ("refused: the simulation does not show the JournalLog emit the "
                                    "entry"))

    if intent.via_account and sender:
        _treasury_why = _tg._treasury_side_refusal(intent, deltas, holder=holder, sender=sender)
        if _treasury_why:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"account: {_treasury_why}")
            return Decision(False, f"refused: {_treasury_why}")

    # 068 N1: a run bound to a target acquires only the target — checked on what
    # the simulation shows ARRIVING, so a generic call or a dapp transaction that
    # never named its acquisition cannot slip another token or an NFT in.
    _target_why = _tg._simulated_target_refusal(execution_context, intent, deltas, holder)
    if _target_why:
        _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                  detail="target_token")
        return Decision(False, _target_why)
    c.deltas = deltas
    return None


def step_6_assert_deltas(c: GuardContext) -> Optional[Decision]:
    """6. Assert the deltas against the declaration."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    outer_tx = c.outer_tx
    deltas = c.deltas
    # -- 6. Assert the deltas against the declaration ----------------------
    outflow_raw = 0
    deploy_facts = None
    position_token_id = None
    lp_implied = False
    registered_agent_id = None  # set by rule 6f when a registration is proven
    if intent.is_deploy:
        # A CREATE (042). There is no counterparty to assert against, so the
        # assertions are about the CODE it produces and about the constructor
        # moving nothing it did not declare — measured in deploy_guard, decided
        # here. The native branch below cannot serve: it refuses a zero outflow
        # as a measurement failure, and a deployment endowing nothing is the
        # normal case, not a broken measurement.
        from core.wallet import deploy_guard as _deploy_guard

        deploy_facts, why = _deploy_guard.assert_deploy(
            intent, deltas, holder=holder, nonce=outer_tx.get("nonce"))
        if why:
            return Decision(False, why)
        moved = int(deltas.native_delta)
        if moved > 0:
            return Decision(False, (
                f"refused: the constructor returns {moved} wei to the wallet — a "
                f"deployment that pays the deployer is not the transaction that "
                f"was declared"))
        outflow_raw = -moved
        if outflow_raw > intent.amount_raw + _tg._NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the deployment moves {outflow_raw} wei but declared "
                f"{intent.amount_raw} — the constructor spends more than it was "
                f"endowed with"))
    elif intent.is_liquidity_op:
        from core.wallet import liquidity_guard
        try:
            position_token_id = liquidity_guard.assert_deltas(intent, deltas, _tg._NATIVE_DUST_WEI)
        except (ValueError, TypeError) as exc:
            return Decision(False, f"refused: {exc}")
    elif intent.token:
        moved = deltas.token_deltas.get(intent.token)
        if moved is None:
            return Decision(False, "refused: simulation did not measure the token being sent")
        if intent.is_allowance_op:
            # An approve/revoke changes an allowance and must move NOTHING.
            # (Before this branch the zero delta below read as a measurement
            # failure, so with a pinned RPC every approve/revoke was dead on
            # arrival — the second DOA hole on this path; the first was the
            # structural amount>0 rule, fixed by is_allowance_op itself.)
            if moved != 0:
                return Decision(False, (
                    f"refused: an allowance operation moved {moved} of the token — "
                    f"an approve/revoke must not transfer funds"))
        else:
            if moved > 0:
                return Decision(False, "refused: simulation shows an INFLOW for a send")
            if moved == 0:
                # A send that moves nothing means the MEASUREMENT failed, not that
                # the transfer is free. This exact hole broadcast a live 0.25 USDC
                # transfer on 2026-08-09: eth_call does not persist state, so every
                # delta read back as 0, the outflow priced at $0.00, and it sailed
                # through a cap that should have refused it. Zero is never a cheap
                # transfer — it is an unmeasured one.
                return Decision(False, (
                    "refused: the simulation measured NO outflow for a transfer that "
                    "should move funds — treating that as a measurement failure, not "
                    "as a free transaction"))
            outflow_raw = -moved
            if outflow_raw > intent.amount_raw:
                return Decision(False, (
                    f"refused: simulated outflow {outflow_raw} exceeds the declared "
                    f"amount {intent.amount_raw}"))
    elif intent.is_claim:
        # A CLAIM (2026-09-14). Nothing leaves, so there is no outflow to bound;
        # what must be proven is that the RECEIPT arrived and that nothing else
        # moved. Both are asserted below, against the measurement.
        outflow_raw = 0
        if int(deltas.native_delta) < -_tg._NATIVE_DUST_WEI:
            # CR-L05: a token-only claim used to skip every native assertion,
            # so a "claim" that also drained native value read as a claim.
            return Decision(False, (
                f"refused: the claim moves {-int(deltas.native_delta)} wei of "
                f"native value OUT of the wallet — a claim sends nothing"))
        for l_token, l_moved in (deltas.token_deltas or {}).items():
            if l_moved < 0:
                # A contract that pays you and quietly takes a token is not a
                # claim. Receiving more than declared is fine; giving anything
                # up is not.
                return Decision(False, (
                    f"refused: the claim moves {-l_moved} of token {l_token} OUT "
                    f"of the wallet — a claim sends nothing"))
        if intent.min_native_inflow_wei:
            if deltas.native_delta <= 0:
                return Decision(False, (
                    f"refused: the claim declared a native receipt but the "
                    f"simulation measures a native delta of {deltas.native_delta} "
                    f"wei — nothing arrives"))
            if deltas.native_delta < intent.min_native_inflow_wei:
                return Decision(False, (
                    f"refused: the simulation returns {deltas.native_delta} wei, "
                    f"below the declared minimum {intent.min_native_inflow_wei} — "
                    f"the contract does not pay what it says it owes"))
        if intent.inflow_token is not None and intent.min_inflow_raw:
            got = (deltas.token_deltas or {}).get(intent.inflow_token)
            if got is None:
                return Decision(False, (
                    f"refused: the simulation did not measure token "
                    f"{intent.inflow_token}, so the declared receipt is unproven"))
            if got < intent.min_inflow_raw:
                return Decision(False, (
                    f"refused: the claim returns {got} of {intent.inflow_token}, "
                    f"below the declared minimum {intent.min_inflow_raw}"))
    elif intent.is_collection_mint:
        # The collection paid mint (F2): exactly the price leaves, exactly
        # the declared ids arrive from 0x0, and nothing else of the signer's moves or is
        # named (core.wallet.collection_mint). The value is what the caps are charged below.
        from core.wallet import collection_mint as _collection_mint
        _mint_err = _collection_mint.assert_simulation(intent, deltas, holder)
        if _mint_err:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"collection_mint: {_mint_err}")
            return Decision(False, f"refused: {_mint_err}")
        outflow_raw = int(intent.amount_raw)
    elif intent.is_collection_reveal:
        # The collection reveal. Nothing moves, so the RECEIPT is asserted:
        # one Revealed/Recommitted per declared id, every log from the collection,
        # nothing naming the signer (core.wallet.collection_reveal).
        from core.wallet import collection_reveal as _collection_reveal
        _reveal_err, _revealed, _recommitted = _collection_reveal.assert_simulation(
            intent, deltas, holder)
        if _reveal_err:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"collection_reveal: {_reveal_err}")
            return Decision(False, f"refused: {_reveal_err}")
    elif intent.is_registration or intent.is_journal_entry:
        # (A journal entry, J1, moves nothing either — its own assertions run above.)
        # Registering sends NO value, so the native branch below cannot serve --
        # it refuses a zero outflow as a measurement failure, and moving nothing
        # is this shape's normal case (same reasoning as is_deploy/is_nft_op).
        # What IS asserted is the RECEIPT, in rule 6f. Here: nothing may leave.
        if abs(int(deltas.native_delta)) > _tg._NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the registration moved {deltas.native_delta} wei of "
                f"native value -- `register()` is not payable on the reference "
                f"registries, so unexplained native movement is not this "
                f"transaction"))
    elif intent.is_nft_op:
        # A non-fungible move (2026-09-15). Nothing fungible leaves, so the
        # native branch below cannot serve — it refuses a zero outflow as a
        # measurement failure, and moving nothing fungible is this shape's
        # NORMAL case, not a broken measurement (same reasoning as is_deploy).
        #
        # What IS asserted: no material native movement. `safeTransferFrom`
        # is not payable in any standard implementation, so native leaving here
        # means the call did something the intent never described. The token
        # side is asserted by rules 6c/6d below, and rule 6b already refuses
        # any undeclared FUNGIBLE transfer out.
        if abs(int(deltas.native_delta)) > _tg._NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: an NFT transfer moved {deltas.native_delta} wei of "
                f"native value — the intent declares a collectible move and "
                f"nothing else, so unexplained native movement is not this "
                f"transaction"))
    elif intent.is_allowance_op:
        # An allowance is a claim on a TOKEN. A native asset has no allowance to
        # grant or revoke, so this combination is a caller bug, not a shape to
        # interpret.
        return Decision(False, (
            "refused: an allowance operation needs a token — a native asset has no "
            "allowance to grant or revoke"))
    else:
        # NATIVE send (039 B1). `intent.token is None` is a DECLARATION, not an
        # absence. Before this branch the guard was ERC-20-only by construction:
        # the outflow assertions above are keyed on `intent.token`, so a native
        # send skipped them all and then hit the blanket native-dust refusal
        # below — which is why `bridge_verb.py` carried a bare "an EVM-origin
        # bridge is not wired yet". A Relay EVM deposit IS a native-value call.
        #
        # Fee and principal are the SAME ASSET here, exactly as they are on the
        # Solana leg, so an exact match cannot be demanded. The tolerance is the
        # SAME pinned dust constant the ERC-20 branch uses below — deliberately
        # not a new number, and deliberately symmetric: a short move and a long
        # move are both "not the transaction that was priced".
        # The value goes to whoever the call targets. A plain native send must target the
        # declared destination, or the owner (and the ledger) see one recipient while the
        # signed transaction pays another. A swap-shaped call (a measured inflow) names
        # its router in `to` and is bound by that inflow instead.
        if not intent.inflow_token and not _tg._same_addr(tx.get("to"), intent.to):
            return Decision(False, (
                f"refused: the transaction sends native value to {tx.get('to')}, not to the "
                f"declared destination {intent.to}"))
        moved = int(deltas.native_delta)
        if moved > 0:
            return Decision(False, "refused: simulation shows a native INFLOW for a send")
        if moved == 0:
            # Same lesson as the token branch, restated for native: a live 0.25
            # USDC transfer broadcast on 2026-08-09 because every delta read back
            # 0 and the outflow priced at $0.00. Zero is never a free transaction
            # — it is an unmeasured one.
            return Decision(False, (
                "refused: the simulation measured NO native outflow for a send that "
                "should move funds — treating that as a measurement failure, not as "
                "a free transaction"))
        outflow_raw = -moved
        if outflow_raw < intent.amount_raw - _tg._NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the simulation moves only {outflow_raw} wei of the declared "
                f"{intent.amount_raw} — a send that moves materially less than it "
                f"declared is not the transaction that was priced"))
        if outflow_raw > intent.amount_raw + _tg._NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: simulated native outflow {outflow_raw} exceeds the declared "
                f"amount {intent.amount_raw} by more than fee dust"))

    declared = {(t.lower(), s.lower()): a for (t, s, a) in intent.expected_allowance_grants}
    for (token, spender), change in deltas.allowance_deltas.items():
        if change <= 0:
            continue
        permitted = declared.get((token.lower(), spender.lower()))
        if permitted is None:
            return Decision(False, (
                f"refused: UNDECLARED allowance grant of {change} on {token} to "
                f"{spender} — a hidden approve is not bounded by a USD cap, because "
                f"the drain happens in a later transaction"))
        if change > permitted:
            return Decision(False, (
                f"refused: allowance grant {change} exceeds the declared {permitted}"))

    if intent.via_account_batch:
        # W8: the reset leg must leave NOTHING standing — absolute, not a delta (an allowance
        # that stood before the batch reads as a decrease and would pass a delta check).
        _key = next((k for k in (getattr(deltas, "allowance_after", None) or {})
                     if k[0].lower() == intent.token.lower()
                     and k[1].lower() == str(intent.to).lower()), None)
        if _key is None:
            return Decision(False, (
                "refused: the simulation did not measure the spender's final allowance — "
                "the batch's reset cannot be proven, so it is not assumed"))
        if int(deltas.allowance_after[_key]) != 0:
            return Decision(False, (
                f"refused: after the batch {intent.to} still holds an allowance of "
                f"{deltas.allowance_after[_key]} on {intent.token} — an approve-spend-reset "
                f"batch must end at exactly 0"))
    c.deploy_facts = deploy_facts
    c.lp_implied = lp_implied
    c.outflow_raw = outflow_raw
    c.position_token_id = position_token_id
    c.registered_agent_id = registered_agent_id
    return None


def step_6a_min_inflow(c: GuardContext) -> Optional[Decision]:
    """6a. Assert the declared MINIMUM INFLOW."""
    intent = c.intent
    deltas = c.deltas
    # -- 6a. Assert the declared MINIMUM INFLOW ---------------------------
    # 042. The other half of the delta contract: everything above bounds what
    # LEAVES, this bounds what must COME BACK. It is what makes caller-supplied
    # calldata assertable at all — a swap/supply/mint call that spends and
    # returns nothing is indistinguishable, to every check above, from one that
    # works.
    #
    # Fail-CLOSED in both directions: a minimum with no token to measure is a
    # caller bug (refuse, never pass vacuously), and an unmeasured inflow is a
    # measurement failure (refuse), exactly as a zero outflow is above.
    if intent.min_inflow_raw is not None:
        if intent.min_inflow_raw <= 0:
            return Decision(False, (
                "refused: a declared minimum inflow must be greater than zero — "
                "a minimum of nothing asserts nothing"))
        if not intent.inflow_token:
            return Decision(False, (
                "refused: a minimum inflow was declared with no inflow_token to "
                "measure it on"))
        received = _tg._measured_inflow_raw(intent, deltas)
        if received is None:
            return Decision(False, (
                f"refused: the simulation measured NO inflow of the declared "
                f"{intent.inflow_token} — the call spends but returns nothing that "
                f"can be observed, which is a measurement failure, not a free gift"))
        if received < intent.min_inflow_raw:
            return Decision(False, (
                f"refused: the simulation returns {received} of "
                f"{intent.inflow_token}, below the declared minimum "
                f"{intent.min_inflow_raw} — the call does not deliver what it "
                f"promised"))
    return None


def step_6b_event_log(c: GuardContext) -> Optional[Decision]:
    """6b. Event-log cross-check."""
    intent = c.intent
    tx = c.tx
    deltas = c.deltas
    # -- 6b. Event-log cross-check ----------------------------------------
    # The reads above cover ONLY the declared token and the measured spenders;
    # a grant to an undeclared spender or a drain of an undeclared token was
    # invisible to them (found in the 2026-08-14 T4 review). The simulated
    # tx's own event log covers whoever it actually touched. Measured pairs
    # are judged by their read delta (authoritative — some tokens re-emit
    # Approval(remaining) on transferFrom, which is a decrease, not a grant);
    # an Approval EVENT on an UNMEASURED pair refuses outright.
    measured_pairs = {(t.lower(), s.lower()) for (t, s) in deltas.allowance_deltas}
    for (l_token, l_spender, l_amount) in deltas.holder_approvals:
        if l_amount <= 0:
            continue                      # setting an allowance to 0 is a revoke
        if (l_token.lower(), l_spender.lower()) in measured_pairs:
            continue                      # the read delta above already judged it
        return Decision(False, (
            f"refused: the transaction emits an UNDECLARED Approval of {l_amount} "
            f"on {l_token} to {l_spender} — a hidden approve is not bounded by a "
            f"USD cap, because the drain happens in a later transaction"))
    for (l_p2, l_token, l_spender, l_amount) in (
            getattr(deltas, "holder_permit2_grants", None) or ()):
        # CR-H06: a Permit2 grant is a standing claim held by Permit2, not an
        # ERC-20 allowance — no read above measures it, and no verb declares
        # one — except the exact, expiring grant to the pinned v4 PositionManager
        # that approve_token(via='permit2') declares (W6). Any other refuses.
        from core.wallet.liquidity_guard import permit2_grant_declared
        if l_amount > 0 and not permit2_grant_declared(
                intent, tx, l_p2, l_token, l_spender, l_amount):
            return Decision(False, (
                f"refused: the transaction emits a Permit2 Approval of "
                f"{l_amount} on {l_token} to {l_spender} (via {l_p2}) — a "
                f"standing claim no read here measures and no verb declares; "
                f"the drain happens in a later transaction"))
    # A TOKEN send: the call does not target the declared destination (a plain send calls
    # the token). Then every Transfer of that token out of the wallet must go to the
    # declared destination, and a call on the token itself must be transfer(to, …) —
    # otherwise a transfer(attacker) passes as "a send to `to`" (the amounts alone match). Swaps (a measured inflow), allowances, LP, claims and unwraps are
    # bound by their own assertions and target what they name.
    if (intent.token and not intent.inflow_token and not intent.is_allowance_op
            and not intent.is_liquidity_op and not intent.is_claim and not intent.is_deploy
            and not intent.min_native_inflow_wei and intent.to
            and not _tg._same_addr(tx.get("to"), intent.to)):
        _paid = False
        for (l_token, l_to, l_amount) in deltas.holder_transfers:
            if not _tg._same_addr(l_token, intent.token) or l_amount <= 0:
                continue
            if not _tg._same_addr(l_to, intent.to):
                return Decision(False, (
                    f"refused: the transaction pays {l_amount} of {intent.token} to "
                    f"{l_to}, not to the declared destination {intent.to}"))
            _paid = True
        # The signed calldata itself: a full-length call must be transfer(to, …) to the
        # declared destination (unless a Transfer to it was measured). A truncated
        # selector-only payload reverts on any real token and moves nothing.
        _cd = str(tx.get("data") or "").lower().removeprefix("0x")
        if _tg._same_addr(tx.get("to"), intent.token) and len(_cd) >= 8 + 128 and not _paid:
            if _cd[:8] != "a9059cbb" or not _tg._same_addr("0x" + _cd[8 + 24:8 + 64], intent.to):
                return Decision(False, (
                    f"refused: the signed call is not transfer({intent.to}, …) on "
                    f"{intent.token} — it does not pay the declared destination"))
    if intent.is_liquidity_op:
        from core.wallet import liquidity_guard
    for (l_token, _l_to, l_amount) in deltas.holder_transfers:
        if intent.is_liquidity_op and l_token.lower() in liquidity_guard.legs(intent.lp_outflows):
            continue
        if intent.token and l_token.lower() == intent.token.lower():
            continue                      # the declared outflow, asserted above
        if l_amount > 0:
            return Decision(False, (
                f"refused: the transaction emits a Transfer of {l_amount} from the "
                f"wallet on UNDECLARED token {l_token} — it moves an asset the "
                f"intent never mentioned"))
    return None


def step_6cde_non_fungibles(c: GuardContext) -> Optional[Decision]:
    """6c/6d/6e. Non-fungible cross-check."""
    intent = c.intent
    deltas = c.deltas
    # -- 6c/6d/6e. NON-FUNGIBLE cross-check (2026-09-15) -------------------
    # ⚠️ These run on EVERY transaction, not only the NFT verbs. Until the
    # simulation learned the 4-topic shapes, `defi_trade.call` / `dapp_connect`
    # could move a collectible out of the treasury and NOTHING above would fire:
    # the reads cover only declared fungibles, and the log parser skipped the
    # event entirely. This is the refusal that closes that.
    _declared_out = {(c.lower(), str(s).lower(), int(i), int(a))
                     for (c, s, i, a) in intent.nft_out}
    for (l_c, l_std, _l_to, l_id, l_amt) in deltas.holder_nft_out:
        if intent.is_liquidity_op and intent.lp_position_effect == "burn":
            continue  # exact burn, including zero recipient, asserted above
        if (l_c.lower(), l_std, int(l_id), int(l_amt)) in _declared_out:
            continue
        return Decision(False, (
            f"refused: the transaction moves an UNDECLARED non-fungible out of "
            f"the wallet — {l_std} {l_c} #{l_id} x{l_amt}. A collectible has no "
            f"price, so no USD cap can bound this; it must be declared"))

    # 6d — a DECLARED move must actually be measured. Broadcasting on a promise
    # is what the 042 min-inflow assertion ended for fungibles.
    _emitted_out = {(c.lower(), s, int(i), int(a))
                    for (c, s, _t, i, a) in deltas.holder_nft_out}
    for (c, s, i, a) in intent.nft_out:
        if (c.lower(), str(s).lower(), int(i), int(a)) not in _emitted_out:
            return Decision(False, (
                f"refused: the intent declares it sends {s} {c} #{i} x{a}, but "
                f"the simulation does not move it — the transaction does not do "
                f"what the intent says"))
    _emitted_in = {(c.lower(), s, int(i), int(a))
                   for (c, s, _t, i, a) in deltas.holder_nft_in}
    for (c, s, i, a) in intent.expected_nft_in:
        if (c.lower(), str(s).lower(), int(i), int(a)) not in _emitted_in:
            return Decision(False, (
                f"refused: the intent declares it receives {s} {c} #{i} x{a}, "
                f"but the simulation shows it does not arrive"))

    # W3 — a DECLARED revoke must be measured, exactly like a declared move (6d).
    _cleared = {(str(c).lower(), int(i)) for (c, to, i) in deltas.holder_nft_approvals
                if str(to).lower() == _tg._ZERO_ADDRESS}
    for (c, i) in intent.nft_approval_revokes:
        if (str(c).lower(), int(i)) not in _cleared:
            return Decision(False, (
                f"refused: the intent declares it clears the approval of {c} #{i}, but "
                f"the simulation emits no Approval(holder, 0x0, {i}) from it — the "
                f"transaction does not do what the intent says"))
    _zeroed_6909 = {(str(c).lower(), str(sp).lower(), int(i))
                    for (c, sp, i, amt) in (getattr(deltas, "holder_6909_approvals", None) or ())
                    if int(amt) == 0}
    for (c, sp, i) in intent.erc6909_revokes:
        if (str(c).lower(), str(sp).lower(), int(i)) not in _zeroed_6909:
            return Decision(False, (
                f"refused: the intent declares it revokes the ERC-6909 allowance of {c} #{i} to "
                f"{sp}, but the simulation emits no Approval(holder, {sp}, {i}, 0) from it"))
    if intent.permit2_revokes:
        from core.wallet import erc6551 as _erc6551
        _p2_zeroed = {(str(t).lower(), str(sp).lower())
                      for (p2, t, sp, amt) in (getattr(deltas, "holder_permit2_grants", None) or ())
                      if str(p2).lower() == _erc6551.PERMIT2.lower() and int(amt) == 0}
        for (t, sp) in intent.permit2_revokes:
            if (str(t).lower(), str(sp).lower()) not in _p2_zeroed:
                return Decision(False, (
                    f"refused: the intent declares it revokes the Permit2 allowance of "
                    f"{t} to {sp}, but the simulation shows Permit2 emit no zero "
                    f"Approval/Lockdown for that pair"))

    # 6e — the blanket grant. A grant can never be declared (step 3 refuses a
    # True in `nft_operator_ops`), so ANY observed grant is undeclared by
    # construction. A revoke only retires a claim and is always fine.
    for (l_c, l_op, l_approved) in deltas.holder_operator_grants:
        if l_approved:
            return Decision(False, (
                f"refused: the transaction emits ApprovalForAll({l_op}, true) on "
                f"{l_c} — a blanket operator approval over the whole collection, "
                f"present and future holdings alike. The drain happens in a "
                f"later transaction no cap here can see"))
    for (l_c, l_sp, l_id, l_amt) in (getattr(deltas, "holder_6909_approvals", None) or ()):
        if int(l_amt) > 0:
            return Decision(False, (
                f"refused: the transaction emits an ERC-6909 Approval of {l_c} #{l_id} to "
                f"{l_sp} for {l_amt} — a standing claim no cap here can see; a grant can never "
                f"be declared"))
    _declared_nft_approvals = {(c.lower(), str(o).lower())
                               for (c, o, _v) in intent.nft_operator_ops}
    for (l_c, l_to, l_id) in deltas.holder_nft_approvals:
        if (intent.is_liquidity_op and intent.lp_position_effect == "burn"
                and l_c.lower() == intent.lp_position[0].lower()
                and l_id == intent.lp_position[1] and l_to.lower() == _tg._ZERO_ADDRESS):
            continue  # ERC721._burn clears this token's approval
        if l_to.lower() == _tg._ZERO_ADDRESS:
            # CR-L16: Approval(owner, 0x0, id) CLEARS the token's approval —
            # OZ < 4.8 and the Uniswap NPM emit it on every transfer. It
            # retires a claim and grants nothing.
            continue
        if (l_c.lower(), l_to.lower()) in _declared_nft_approvals:
            continue
        return Decision(False, (
            f"refused: the transaction emits an UNDECLARED Approval of "
            f"{l_c} #{l_id} to {l_to} — a standing claim on that token"))
    return None


def step_6g_account_nesting(c: GuardContext) -> Optional[Decision]:
    """6g. No token inside an account of its own collection."""
    intent = c.intent
    tx = c.tx
    execution_context = c.execution_context
    tool_self = c.tool_self
    account_rpc = c.account_rpc
    deltas = c.deltas
    # -- 6g. No token inside an account of its own collection (W2) ---------
    # contracts/SPEC.md §7.2: `transferFrom(alice, accountOf(id), id)` bricks a
    # token and its account forever (AccountV3 refuses its own token only in
    # onERC721Received, which transferFrom never calls), and two tokens can own
    # each other. The contract refuses it in `_update`; this is the mirror for
    # every NFT the guarded wallet moves — measured moves, any verb, any shape.
    _nest = _tg._collection_account_destination_refusal(intent.chain, tx, deltas.holder_nft_out)
    if not _nest and deltas.holder_nft_out:
        _nest = _tg._nft_nesting_refusal(
            intent.chain, deltas.holder_nft_out,
            account_rpc or _tg.simulation._default_rpc_for(intent.chain))
    if _nest:
        _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {_nest}")
        return Decision(False, f"refused: {_nest}")
    return None


def step_6g2_future_account(c: GuardContext) -> Optional[Decision]:
    """6g''. No asset into the account of a token not minted yet."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    account_rpc = c.account_rpc
    deltas = c.deltas
    # -- 6g''. C5: no asset of ANY kind into the account of a token not minted yet. Whoever mints
    # k later owns what sits in accountOf(k) — ETH, ERC-20 or NFT alike.
    _future = _tg._future_account_refusal(
        intent, tx, deltas, holder, account_rpc or _tg.simulation._default_rpc_for(intent.chain))
    if _future:
        _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {_future}")
        return Decision(False, f"refused: {_future}")
    return None


def step_6g1_pinned_departure(c: GuardContext) -> Optional[Decision]:
    """6g'. A pinned collection's token leaves only clean."""
    intent = c.intent
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    account_rpc = c.account_rpc
    deltas = c.deltas
    # -- 6g'. A pinned collection's token leaves only with its code pinned and its account
    # carrying NO open approval (069 v4 §5 rules 3 and 5). Approvals survive a transfer of the
    # NFT: a token that leaves with one open hands the next owner an account a stranger can
    # still drain. No override for a row an NFT owner's transaction granted (the remedy is
    # agent_nft_revoke_all); the owner may accept only a row no owner's transaction emitted
    # (possibly fabricated). Fail closed on an incomplete scan.
    if deltas.holder_nft_out:
        _gone = _tg._pinned_nft_departure_refusal(
            intent.chain, deltas.holder_nft_out,
            account_rpc or _tg.simulation._default_rpc_for(intent.chain), holder=holder,
            accepted=tuple(getattr(intent, "accepted_unattributed_approvals", ()) or ()))
        if _gone:
            _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {_gone}")
            return Decision(False, f"refused: {_gone}")
    return None


def step_6f_registration_receipt(c: GuardContext) -> Optional[Decision]:
    """6f. The REGISTRATION receipt."""
    intent = c.intent
    deltas = c.deltas
    # -- 6f. The REGISTRATION receipt (046) -------------------------------
    # (initialised above the branch so every later return can carry it)
    # ⚠️ The whole point of the shape. `register()` transfers no value, so
    # every check above passes for a transaction that did nothing but burn gas.
    # The ERC-8004 Identity Registry is an ERC-721 and `register()` MINTS --
    # `agentId` IS the tokenId -- so a genuine registration always emits
    # Transfer(from=0x0, to=us) from the registry itself. Absence refuses.
    registered_agent_id = None
    if intent.is_registration:
        want = (intent.expected_registry or "").lower()
        minted = [e for e in deltas.holder_nft_in
                  if e[0].lower() == want and e[2].lower() == _tg._ZERO_ADDRESS.lower()]
        if not intent.expects_mint:
            # An UPDATE. The inverse assertion, and the one that matters: a
            # transaction that claims to change the published file while
            # actually minting has created a second identity.
            if minted:
                return Decision(False, (
                    f"refused: this transaction updates the registration file "
                    f"but MINTS agent token {minted[0][3]} -- an update that "
                    f"mints leaves two agentIds and no way to say which "
                    f"identity is authoritative"))
            if any(e[0].lower() == want for e in deltas.holder_nft_in):
                return Decision(False, (
                    "refused: this transaction receives an agent token while "
                    "claiming only to update the registration file"))
        elif not minted:
            from_other = [e for e in deltas.holder_nft_in if e[0].lower() == want]
            if from_other:
                # Somebody TRANSFERRED us an existing agent token. That is
                # someone handing over THEIR identity, not us registering ours,
                # and it is emphatically not what the caller asked for.
                return Decision(False, (
                    f"refused: the registry {intent.expected_registry} transfers "
                    f"an EXISTING agent token to this wallet rather than MINTING "
                    f"a new one -- that is receiving somebody else's identity, "
                    f"not registering"))
            return Decision(False, (
                f"refused: the transaction does not mint an agent token from "
                f"{intent.expected_registry}. A registration that mints nothing "
                f"is a contract call that consumed gas, and nothing above can "
                f"tell the two apart -- which is why this check exists"))
        elif len(minted) > 1:
            return Decision(False, (
                f"refused: the transaction mints {len(minted)} agent tokens; a "
                f"registration mints exactly one identity"))
        if minted and intent.expects_mint:
            # Only a MINT yields an id to report. An update mints nothing, so
            # reporting one would be inventing a fact.
            registered_agent_id = int(minted[0][3])
    c.registered_agent_id = registered_agent_id
    return None


def step_6_native_balance(c: GuardContext) -> Optional[Decision]:
    """6 (tail). The native receipt / unexpected native movement."""
    intent = c.intent
    deltas = c.deltas
    # ERC-20 ONLY, as this check's own constant always said: "native balance is
    # not expected to move on an ERC-20 transfer". A NATIVE send declares that
    # move, and the branch above asserts it against the declared amount — running
    # this check on it would refuse every honest native transaction.
    if intent.token and intent.min_native_inflow_wei is not None:
        # A DECLARED native receipt (042): a sell into a native-quoted venue.
        # Asserted, not waved through — the measurement decides.
        if intent.min_native_inflow_wei <= 0:
            return Decision(False, (
                "refused: a declared minimum native inflow must be greater than "
                "zero — a minimum of nothing asserts nothing"))
        if deltas.native_delta <= 0:
            return Decision(False, (
                f"refused: the transaction declared a native RECEIPT but the "
                f"simulation measures a native delta of {deltas.native_delta} "
                f"wei — the sale returns nothing observable"))
        if deltas.native_delta < intent.min_native_inflow_wei:
            return Decision(False, (
                f"refused: the simulation returns {deltas.native_delta} wei, "
                f"below the declared minimum {intent.min_native_inflow_wei} — "
                f"the trade does not deliver what it promised"))
    elif intent.token and abs(deltas.native_delta) > _tg._NATIVE_DUST_WEI:
        return Decision(False, (
            f"refused: unexpected native balance change of {deltas.native_delta} wei — "
            f"the transaction does something that was not declared"))
    return None
