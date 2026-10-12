"""tx_guard steps 3-4: the structural checks, before any RPC, and the RPC trust pins.

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


def step_3_structural(c: GuardContext) -> Optional[Decision]:
    """3. Structural: amount and liquidity shape."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    # -- 3. Structural -----------------------------------------------------
    if intent.amount_raw < 0:
        return Decision(False, "refused: amount cannot be negative")
    if intent.amount_raw == 0 and not (intent.is_allowance_op or intent.is_deploy
                                       or intent.is_claim or intent.is_nft_op
                                       or intent.is_registration or intent.is_liquidity_op
                                       or intent.is_collection_reveal or intent.is_journal_entry):
        # A zero-value transfer is meaningless. An allowance op legitimately
        # moves nothing and must say so explicitly — it is never inferred. A
        # deployment commonly endows the constructor with nothing at all, and
        # its risk is the fee and the code, not a transferred amount. A CLAIM
        # sends nothing by definition; its whole shape is the receipt. An NFT
        # op moves an IDENTIFIER, which no amount can describe.
        return Decision(False, "refused: amount must be greater than zero")

    if intent.is_liquidity_op:
        if execution_context is not None and (getattr(execution_context, "is_sub_agent", False)
                or getattr(execution_context, "role", None) == "leaf"):
            return Decision(False, "refused: a delegated leaf cannot change liquidity")
        from core.wallet import liquidity_guard
        try:
            # `holder` is the account under `via_account` (step 2b): a collect
            # or a mint must pay the address whose balances are measured.
            liquidity_guard.structural(intent, tx, holder=holder)
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            return Decision(False, f"refused: {exc}")
    return None


def step_3a_lifi_floor(c: GuardContext) -> Optional[Decision]:
    """3a. The on-chain floor of a LI.FI diamond swap."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    # -- 3a. The on-chain floor of a LI.FI diamond swap (WAL-7) ------------
    # The measured-inflow floor (6a) is judged on a simulation, which a contract
    # can tell apart from the real block. For a LI.FI facet whose layout is
    # known, the minimum the CHAIN enforces is read from the signed bytes and
    # held to the declared floor, and its receiver to the wallet. An unknown
    # facet is not refused: 6a still applies.
    if intent.min_inflow_raw and intent.inflow_token:
        from core.wallet import chains as _chains, lifi_calldata
        _diamond = (getattr(_chains.get(intent.chain), "aggregator_spender", "") or "").lower()
        if _diamond and str(tx.get("to") or "").lower() == _diamond:
            _lifi_why = lifi_calldata.floor_refusal(
                tx.get("data") or tx.get("input"), receiver=holder,
                token_out=intent.inflow_token, floor=intent.min_inflow_raw)
            if _lifi_why:
                _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                          detail=f"lifi: {_lifi_why}")
                return Decision(False, f"refused: {_lifi_why}")
    return None


def step_3_collection_shapes(c: GuardContext) -> Optional[Decision]:
    """3. Structural: collection reveal / mint shapes."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    if intent.is_collection_reveal:
        # A pinned collection (reveal capability), value 0, exactly reveal(uint256[]) for the
        # declared ids, no other shape. Checked here, before any RPC.
        from core.wallet import collection_reveal as _collection_reveal
        _reveal_err = _collection_reveal.structural(intent, tx)
        if _reveal_err:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"collection_reveal: {_reveal_err}")
            return Decision(False, f"refused: {_reveal_err}")
    elif intent.reveal_ids:
        return Decision(False, "refused: reveal ids were declared without is_collection_reveal")

    if intent.is_collection_mint:
        # A pinned collection (mint capability), exactly mint(treasury, qty, min) for the declared
        # consecutive ids, value == amount_raw == qty × PRICE, no other shape. Before any RPC.
        from core.wallet import collection_mint as _collection_mint
        _mint_err = _collection_mint.structural(intent, tx, holder)
        if _mint_err:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"collection_mint: {_mint_err}")
            return Decision(False, f"refused: {_mint_err}")
    elif intent.mint_ids:
        return Decision(False, "refused: mint ids were declared without is_collection_mint")
    return None


def step_3_registration_shape(c: GuardContext) -> Optional[Decision]:
    """3. Structural: agent registration shape."""
    intent = c.intent
    tx = c.tx
    if intent.is_registration:
        # Declared and PINNED, never inferred, and checked HERE so a malformed
        # one cannot reach a measurement branch written for another shape.
        if intent.is_deploy or intent.is_claim or intent.is_nft_op:
            return Decision(False, (
                "refused: a transaction cannot be both an agent REGISTRATION "
                "and another shape -- they assert different things about what "
                "moves and what arrives"))
        if not intent.expected_registry:
            return Decision(False, (
                "refused: a registration must name the registry that will mint "
                "the agent token (`expected_registry`, from the pinned "
                "core.wallet.erc8004 table). One that declares no registry "
                "asserts nothing, and is an arbitrary contract call wearing a "
                "registration's name"))
        # ⚠️ Check the TRANSACTION's destination, not the intent's. The intent
        # is the caller's claim; `tx["to"]` is what actually gets signed, and
        # comparing the claim against itself asserts precisely nothing. (Caught
        # by its own test: the first version of this rule did exactly that and
        # happily authorized a transaction addressed somewhere else entirely.)
        # The declared registry must BE the pinned one for this chain: binding `tx.to`
        # to a caller-declared value alone would let any contract wear the name.
        try:
            from core.wallet import erc8004 as _erc8004
            _pinned_registry = _erc8004.resolve_identity_registry(intent.chain).lower()
        except Exception as exc:  # noqa: BLE001 — no pinned registry: fail closed
            return Decision(False, (
                f"refused: no pinned ERC-8004 identity registry for {intent.chain} ({exc})"))
        if intent.expected_registry.lower() != _pinned_registry:
            return Decision(False, (
                f"refused: {intent.expected_registry} is not the pinned ERC-8004 identity "
                f"registry for {intent.chain}"))
        _want_registry = intent.expected_registry.lower()
        _tx_to = str((tx or {}).get("to") or "").lower()
        if intent.to and intent.to.lower() != _want_registry:
            return Decision(False, (
                f"refused: the intent is addressed to {intent.to} but the "
                f"declared ERC-8004 registry is {intent.expected_registry} -- "
                f"a registration may only ever call its pinned registry"))
        if _tx_to != _want_registry:
            return Decision(False, (
                f"refused: the TRANSACTION is addressed to {_tx_to or '(none)'} "
                f"but the declared ERC-8004 registry is "
                f"{intent.expected_registry} -- the signed destination is what "
                f"matters, and it is not the pinned registry"))
    return None


def step_3_nft_and_revoke_shapes(c: GuardContext) -> Optional[Decision]:
    """3. Structural: NFT op and revoke shapes."""
    intent = c.intent
    tx = c.tx
    if intent.is_nft_op:
        # Declared, never inferred — and checked HERE so a malformed one cannot
        # reach a measurement branch written for another shape and be waved
        # through by it.
        if intent.is_deploy or intent.is_claim:
            return Decision(False, (
                "refused: a transaction cannot be both an NFT operation and a "
                "DEPLOYMENT or a CLAIM — they assert different things about "
                "what moves"))
        if not (intent.nft_out or intent.expected_nft_in
                or intent.nft_operator_ops or intent.nft_approval_revokes
                or intent.erc6909_revokes):
            return Decision(False, (
                "refused: an NFT operation must declare what leaves "
                "(`nft_out`), what must arrive (`expected_nft_in`), or the "
                "approval it retires — one that declares nothing asserts "
                "nothing, and is an arbitrary call wearing an NFT verb's name"))
        for _c, _op, _approved in intent.nft_operator_ops:
            if _approved:
                return Decision(False, (
                    f"refused: a transaction may never GRANT ApprovalForAll "
                    f"({_op} on {_c}) — a blanket operator approval is a "
                    f"standing claim on every token of the collection, present "
                    f"and future, and no simulation can bound what it later "
                    f"enables. Only a revoke (approved=False) may be declared"))

    if intent.erc6909_revokes and not intent.is_nft_op:
        return Decision(False, (
            "refused: an ERC-6909 revoke is an NFT operation — declare is_nft_op=True with it"))
    if intent.nft_approval_revokes and not intent.is_nft_op:
        return Decision(False, (
            "refused: a single-token ERC-721 revoke is an NFT operation — declare "
            "is_nft_op=True with it"))
    if intent.permit2_revokes:
        from core.wallet import erc6551 as _erc6551
        _p2 = _erc6551.PERMIT2.lower()
        if not intent.is_allowance_op or intent.expected_allowance_grants:
            return Decision(False, (
                "refused: a Permit2 revoke is an allowance operation that grants "
                "nothing — declare is_allowance_op=True and no grants"))
        if (intent.to or "").lower() != _p2 or str((tx or {}).get("to") or "").lower() != _p2:
            return Decision(False, (
                f"refused: a Permit2 revoke must be addressed to the pinned Permit2 "
                f"{_erc6551.PERMIT2} (intent {intent.to!r}, transaction {tx.get('to')!r})"))
        if not intent.token or intent.token.lower() not in {
                str(t).lower() for (t, _s) in intent.permit2_revokes}:
            return Decision(False, (
                "refused: a Permit2 revoke must declare `token` = one of the tokens it "
                "revokes, so the simulation measures that nothing of it moves"))

    if (intent.is_allowance_op and not intent.expected_allowance_grants
            and not intent.permit2_revokes and not intent.is_nft_op):
        # An ERC-20 REVOKE is priced at $0 and treated as exit-shaped, so its shape is
        # asserted exactly: `token.approve(to, 0)` — the declared token, the declared
        # spender, zero. Without this any zero-movement call (ownership transfer,
        # operator or delegation grant on another contract) passed as "a revoke".
        _data = str((tx or {}).get("data") or "").lower().removeprefix("0x")
        _want = ("095ea7b3" + "0" * 24 + str(intent.to or "").lower().removeprefix("0x")
                 + "0" * 64)
        if (not intent.token or not _tg._same_addr((tx or {}).get("to"), intent.token)
                or _data != _want or int((tx or {}).get("value") or 0) != 0):
            return Decision(False, (
                f"refused: a revoke must be exactly approve({intent.to}, 0) on the declared "
                f"token {intent.token} with no value — this transaction is not that call"))
    return None


def step_3_claim_deploy_destination(c: GuardContext) -> Optional[Decision]:
    """3. Structural: claim, deploy and destination."""
    intent = c.intent
    tx = c.tx
    if intent.is_claim:
        # A claim is declared, never inferred, and the declaration is checked
        # HERE so a malformed one cannot reach the measurement branches and be
        # waved through by a rule that was written for another shape.
        if intent.is_deploy:
            return Decision(False, (
                "refused: a transaction cannot be both a CLAIM and a DEPLOYMENT "
                "— they assert opposite things about the destination"))
        if intent.is_allowance_op:
            return Decision(False, (
                "refused: a transaction cannot be both a CLAIM and an allowance "
                "operation"))
        if not intent.to:
            return Decision(False, (
                "refused: a claim must name the contract it claims FROM"))
        if str(tx.get("to") or "").lower() != intent.to.lower():
            # The guard authorizes `intent.to`; the rail signs `tx["to"]`. The
            # deploy paths cross-check these and nothing else did. It matters
            # most here because `launchpad_claim` PRINTS a provenance sentence
            # about its destination ("read FROM the curve, not supplied"), and
            # that has to be true of the transaction that gets signed.
            return Decision(False, (
                f"refused: the claim declares {intent.to} but the transaction "
                f"is addressed to {tx.get('to')!r} — the authorized destination "
                f"and the signed one must be the same contract"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a claim declares no outflow, so it carries no outflow "
                "token — name what you EXPECT TO RECEIVE with inflow_token "
                "instead"))
        if intent.amount_raw != 0:
            return Decision(False, (
                f"refused: a claim declared an outflow of {intent.amount_raw} — "
                f"a claim sends nothing, and value that leaves belongs in a "
                f"transfer or a swap where it can be priced"))
        declared_native = (intent.min_native_inflow_wei or 0) > 0
        declared_token = (intent.inflow_token is not None
                          and (intent.min_inflow_raw or 0) > 0)
        if not (declared_native or declared_token):
            # The whole point. A claim that declares no minimum asserts nothing,
            # and an unasserted claim is a call to an arbitrary contract wearing
            # a claim's name.
            return Decision(False, (
                "refused: a claim must declare the MINIMUM it expects to "
                "receive — min_native_inflow_wei, or inflow_token with "
                "min_inflow_raw. A claim that asserts nothing is not a claim"))
    if intent.is_deploy and intent.create2_salt:
        # The DETERMINISTIC path. This one IS a call — to the pinned factory and
        # to nothing else, which is the whole of what makes the address a proof.
        from core.wallet import deploy_guard as _dg
        factory = _dg.CREATE2_FACTORY.lower()
        if (intent.to or "").lower() != factory:
            return Decision(False, (
                f"refused: a CREATE2 deployment must be addressed to the pinned "
                f"factory {_dg.CREATE2_FACTORY}, not {intent.to!r}"))
        if str(tx.get("to") or "").lower() != factory:
            return Decision(False, (
                f"refused: the transaction is addressed to {tx.get('to')!r}, not "
                f"the pinned CREATE2 factory the intent declares"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a deployment endows NATIVE value only — declare "
                "token=None"))
    elif intent.is_deploy:
        if intent.to:
            return Decision(False, (
                f"refused: a deployment has no destination, but this intent "
                f"declares {intent.to!r} — a call wearing a deploy's name is "
                f"not a deploy"))
        if tx.get("to"):
            return Decision(False, (
                "refused: the transaction carries a destination, so it is a "
                "CALL, not the CREATE this intent declares"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a deployment endows NATIVE value only — declare "
                "token=None. A token cannot be sent by a create transaction"))
    elif not intent.to:
        return Decision(False, (
            "refused: no destination declared, and this intent is not a deploy"))
    elif intent.to.lower() in (_tg._ZERO_ADDRESS.lower(), _tg._DEAD_ADDRESS.lower()):
        return Decision(False, "refused: destination is the zero/burn address")
    return None


def step_4_rpc_trust(c: GuardContext) -> Optional[Decision]:
    """4. RPC trust and pre-simulation pins."""
    intent = c.intent
    tx = c.tx
    holder = c.holder
    execution_context = c.execution_context
    tool_self = c.tool_self
    rpc_is_pinned_fn = c.rpc_is_pinned_fn
    liquidity_rpc = c.liquidity_rpc
    account_rpc = c.account_rpc
    journal_to = c.journal_to
    sender = c.sender
    # -- 4. RPC trust ------------------------------------------------------
    if not rpc_is_pinned_fn(intent.chain):
        return Decision(False, (
            f"refused: no pinned RPC for {intent.chain}. Simulation, deltas and caps "
            f"all read from it, so the shared public endpoint cannot be the trust "
            f"anchor for moving funds — set {_tg._rpc_env_name(intent.chain)}"))

    if intent.is_collection_mint or intent.is_collection_reveal:
        # 069 v4 A3: the pinned profile's runtime_sha256 is re-checked against the live code
        # before the guard trusts the collection's own events (fail closed).
        _code_why = _tg._collection_runtime_refusal(
            intent.chain, intent.to, account_rpc or _tg.simulation._default_rpc_for(intent.chain))
        if _code_why:
            _tg._record_money_refusal("money_gate", tool_self, execution_context,
                                      detail=f"collection: {_code_why}")
            return Decision(False, f"refused: {_code_why}")

    if intent.via_account:
        preflight = _tg._via_account_preflight(
            intent, sender, account_rpc or _tg.simulation._default_rpc_for(intent.chain),
            inner_to=tx.get("to"), journal_to=journal_to)
        if preflight:
            _tg._record_money_refusal("money_gate", tool_self, execution_context, detail=f"account: {preflight}")
            return Decision(False, f"refused: {preflight}")

    if intent.is_liquidity_op:
        from core.wallet import dex_registry, abi
        from core.wallet import liquidity_guard
        rpc = liquidity_rpc or _tg.simulation._default_rpc_for(intent.chain)
        try:
            liquidity_guard.verify_pins(rpc, intent, tx)   # v3, or v4 + its hook (W6)
            if intent.lp_position_effect != "mint":
                raw = rpc("eth_call", [{"to": intent.to, "data": abi.encode_call(
                    "ownerOf", [{"type": "uint256"}], [intent.lp_position[1]])}, "latest"])
                owner = abi.decode([{"type": "address"}], raw)[0]
                if owner.lower() != holder.lower():
                    raise ValueError("the wallet does not own the liquidity position")
        except Exception as exc:
            return Decision(False, f"refused: liquidity pin/ownership verification failed ({exc})")
    return None
