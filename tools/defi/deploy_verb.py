"""Contract and token deployment (042).

Decomposition note: this lives beside ``bridge_verb.py`` rather than inside
``trade_tool.py`` — new behaviour gets its own module.

Two verbs, deliberately unequal:

* ``deploy_token`` takes NO bytecode. It deploys the one pinned artifact in
  ``core.wallet.token_template``, whose runtime the guard asserts byte for byte,
  so the claims it lets the agent make ("fixed supply", "no mint function") are
  enforced rather than believed.
* ``deploy_contract`` takes caller-supplied init code. It is bounded by the same
  guard — the constructor may not move a token, may not grant an allowance, must
  produce code — but there is nothing to compare the code AGAINST, so what it
  deploys is the caller's problem and the refusals say so.

Neither verb calls a compiler and neither accepts Solidity. Compiling at run
time would mean fetching a toolchain and trusting its output on the money path;
the agent brings bytes it already has, or it uses the template.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Default off. A money verb that ships armed is a money verb that ships wrong.
FLAG = "DEFI_DEPLOY_ENABLED"


def deploy_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


def _refuse_non_owner_turn(execution_context, verb: str) -> Optional[str]:
    """A delegated sub-agent never deploys — it reports back. Fails closed.
    ONE shape for every money verb: ``core.wallet.authority.leaf_refusal``."""
    from core.wallet.authority import leaf_refusal
    return leaf_refusal(execution_context, verb)


def _refuse_paused() -> Optional[str]:
    """The 031 owner pause — ``core.wallet.authority.spend_pause_refusal``."""
    from core.wallet.authority import spend_pause_refusal
    return spend_pause_refusal()



def _resolve_create2(tool, params, init_code: str):
    """``(plan, refusal)`` — the CREATE2 plan, or None for a plain CREATE.

    A plan is ``{salt, address, calldata, attempts}``. Resolving it here rather
    than inside the shared body keeps the mining (which is CPU work with no
    network) out of the guarded section.
    """
    from core.wallet import deploy_guard

    salt = (getattr(params, "salt", "") or "").strip()
    vanity = (getattr(params, "vanity", "") or "").strip()
    if not salt and not vanity:
        return None, None

    attempts = 0
    if vanity:
        if salt:
            return None, ("refused: give a salt OR a vanity prefix, not both — "
                          "mining a salt for a prefix is what produces the salt")
        try:
            salt, address, attempts = deploy_guard.mine_vanity_salt(
                init_code, prefix=vanity)
        except ValueError as exc:
            return None, f"refused: {exc}"
        if salt is None:
            return None, (
                f"refused: no address starting with '{vanity}' was found in "
                f"{attempts} attempts. Ask for a shorter prefix — each extra "
                f"character is 16x the work.")
    else:
        if not salt.startswith("0x"):
            salt = "0x" + salt
        try:
            address = deploy_guard.predict_create2_address(salt, init_code)
        except ValueError as exc:
            return None, f"refused: {exc}"

    plan = {"salt": salt, "address": address, "attempts": attempts,
            "calldata": deploy_guard.create2_calldata(salt, init_code)}
    return plan, None


def _create2_target_is_free(tool, chain: str, address: str):
    """None when nothing is deployed there, else the reason it cannot be used.

    The factory REVERTS on an occupied address, and a bare "the transaction
    reverts in simulation" would leave the caller guessing. Fail-open on a read
    error: the simulation is still the thing that decides.
    """
    try:
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        code = _rpc(rpc_url_for_chain(chain), "eth_getCode",
                    [address, "latest"], timeout=8.0)
    except Exception as exc:
        logger.debug("deploy: could not read code at %s (%s)", address, exc)
        return None
    if isinstance(code, str) and len(code) > 2:
        return (f"refused: {address} already holds a contract. These exact bytes "
                f"with this salt can only be deployed once per chain — change the "
                f"salt (or the vanity prefix) for a different address.")
    return None


async def perform_deploy_token(tool, params, execution_context=None):
    """Deploy the pinned fixed-supply ERC-20 and report the address."""
    from core.wallet import token_template

    if not deploy_enabled():
        return tool._ar(error=(
            f"contract deployment is off — set {FLAG}=true to arm it. Nothing "
            f"was broadcast."))
    try:
        supply_raw = token_template.whole_to_raw(params.supply, params.decimals)
        init_code = token_template.build_init_code(
            name=params.name, symbol=params.symbol,
            decimals=params.decimals, supply_raw=supply_raw)
    except token_template.TokenTemplateError as exc:
        return tool._ar(error=f"refused: {exc}")

    plan, plan_err = _resolve_create2(tool, params, init_code)
    if plan_err:
        return tool._ar(error=plan_err)

    describe = (
        f"deploy token {params.symbol} ({params.name}) on {params.chain}\n"
        f"  supply:   {params.supply:g} whole units "
        f"({supply_raw} raw at {params.decimals} decimals)\n"
        f"  template: FixedSupplyToken — no mint function, no owner, no pause, "
        f"no fee\n")
    return await _perform_deploy(
        tool, execution_context=execution_context, verb="deploy_token",
        chain=params.chain, init_code=init_code, value_wei=0,
        max_spend_usd=params.max_spend_usd, dry_run=params.dry_run,
        describe=describe, create2=plan,
        expected_runtime=token_template.RUNTIME,
        immutable_slots=token_template.IMMUTABLE_SLOTS,
        success_note=(
            "  The whole supply is now held by the wallet. The token has no "
            "mint function, so this is the supply forever.\n"))


async def perform_deploy_contract(tool, params, execution_context=None):
    """Deploy caller-supplied init code."""
    if not deploy_enabled():
        return tool._ar(error=(
            f"contract deployment is off — set {FLAG}=true to arm it. Nothing "
            f"was broadcast."))

    code = (params.bytecode or "").strip()
    if not code.startswith("0x"):
        code = "0x" + code
    try:
        raw = bytes.fromhex(code[2:])
    except ValueError:
        return tool._ar(error=(
            "refused: bytecode is not valid hex. Pass the COMPILED creation "
            "bytecode (init code), not Solidity source — nothing here compiles."))
    if not raw:
        return tool._ar(error="refused: bytecode is empty — there is nothing to deploy")

    args = (params.constructor_args or "").strip()
    if args:
        if args.startswith("0x"):
            args = args[2:]
        try:
            bytes.fromhex(args)
        except ValueError:
            return tool._ar(error=(
                "refused: constructor_args is not valid hex. It must be the "
                "ABI-encoded argument tail, appended to the init code."))
        if len(args) % 64:
            return tool._ar(error=(
                f"refused: constructor_args is {len(args) // 2} bytes, not a "
                f"whole number of 32-byte ABI words — an argument tail that is "
                f"not word-aligned is a mis-encoding, not a short one."))
        code = code + args

    value_wei = int(round(float(params.value or 0.0) * 10 ** 18))
    plan, plan_err = _resolve_create2(tool, params, code)
    if plan_err:
        return tool._ar(error=plan_err)
    if plan is not None and value_wei > 0:
        # The factory forwards `callvalue` to the CREATE2, but the wallet's
        # declared outflow is then against the FACTORY rather than the contract,
        # and nothing here has verified the factory's forwarding on this chain.
        # Refusing beats a plausible-looking assertion nobody checked.
        return tool._ar(error=(
            "refused: an endowed constructor and the deterministic factory are "
            "not combined here. Deploy with value on the ordinary CREATE path, "
            "or deploy deterministically with value=0 and fund it after."))
    describe = (
        f"deploy contract on {params.chain}\n"
        f"  init code: {len(raw)} bytes"
        f"{f' + {len(args) // 2} bytes of constructor args' if args else ''}\n"
        f"  endowment: {params.value:g} native\n"
        f"  ⚠️ caller-supplied bytecode — the guard bounds what the CONSTRUCTOR "
        f"may do, not what the contract will later do\n")
    return await _perform_deploy(
        tool, execution_context=execution_context, verb="deploy_contract",
        chain=params.chain, init_code=code, value_wei=value_wei,
        max_spend_usd=params.max_spend_usd, dry_run=params.dry_run,
        describe=describe, expected_runtime=None, immutable_slots=(),
        create2=plan)


async def _perform_deploy(tool, *, execution_context, verb: str, chain: str,
                          init_code: str, value_wei: int, max_spend_usd: float,
                          dry_run: bool, describe: str,
                          expected_runtime: Optional[str],
                          immutable_slots: Sequence[Tuple[int, int]],
                          success_note: str = "",
                          create2: Optional[dict] = None):
    """The shared body. Mirrors ``wrap``: reserve -> authorize -> size -> send."""
    from core.wallet import deploy_guard, tx_guard, tx_notify
    from core.wallet.broadcast.evm import EvmRail
    from tools.defi.trade_tool import _unsupported_chain

    chain_err = _unsupported_chain(chain)
    if chain_err:
        return tool._ar(error=chain_err)

    turn_err = _refuse_non_owner_turn(execution_context, verb.replace("_", " "))
    if turn_err:
        return tool._ar(error=turn_err)
    if not dry_run:
        paused = _refuse_paused()
        if paused:
            return tool._ar(error=paused + " RESULT: NOT SENT.")

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    signer = wallet.operational_signer()
    gate = wallet.policy
    idem = f"defi_{verb}:{chain}:{uuid.uuid4().hex[:8]}"

    rail = (tool._rail_factory or EvmRail)(chain=chain, signer=signer)
    if create2 is not None:
        occupied = _create2_target_is_free(tool, chain, create2["address"])
        if occupied:
            return tool._ar(error=occupied)
        describe += (
            f"  address:  {create2['address']} — DETERMINISTIC, the same on "
            f"every chain for these bytes\n"
            f"  salt:     {create2['salt']}"
            + (f" (mined in {create2['attempts']} attempts)\n"
               if create2.get("attempts") else "\n"))
        try:
            tx = rail.build_call(to=deploy_guard.CREATE2_FACTORY,
                                 data=create2["calldata"], value=0)
        except Exception as exc:
            return tool._ar(error=f"could not build the deployment: {exc}")
    else:
        try:
            tx = rail.build_deploy(init_code=init_code, value=value_wei)
        except Exception as exc:
            return tool._ar(error=f"could not build the deployment: {exc}")

    intent = tx_guard.TxIntent(
        chain=chain, token=None,
        to=(deploy_guard.CREATE2_FACTORY if create2 is not None else None),
        amount_raw=value_wei,
        max_spend_usd=max_spend_usd, idempotency_key=idem,
        is_deploy=True, init_code=init_code,
        expected_runtime=expected_runtime, immutable_slots=tuple(immutable_slots),
        create2_salt=(create2["salt"] if create2 is not None else None))

    authorize = tool._guard_fn or tx_guard.authorize
    async with gate.reserve():
        from tools.controller.action_registration import _is_forged_or_autonomous_turn

        decision = authorize(intent, tx, holder=signer.address, gate=gate,
                             execution_context=execution_context, tool_self=tool,
                             price_fn=tool._price,
                             forged_fn=_is_forged_or_autonomous_turn)

        facts = decision.deploy
        header = describe
        if facts is not None and create2 is not None:
            # The address was already stated above, and it is NOT nonce-derived
            # here — it is a hash of the init code, which is the whole proof.
            # Repeating it with the other path's wording would describe a
            # different guarantee than the one that was checked.
            header += (f"  proof:    the address commits to the init code "
                       f"({facts.runtime_hash})\n")
        elif facts is not None:
            header += (
                f"  address:  {facts.predicted_address} (predicted from the "
                f"wallet's nonce)\n"
                f"  runtime:  {facts.runtime_size} bytes, {facts.runtime_hash}\n"
                "  template: "
                f"{'MATCHED byte for byte' if facts.template_matched else 'none — caller-supplied bytecode'}\n")
        header += (
            f"  cost:     "
            f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'} "
            f"(endowment + worst-case fee)\n"
            f"  guard:    {decision.reason}\n"
            f"  lane:     {decision.lane}\n")

        if not decision.allowed:
            return tool._ar(content=header + "  RESULT: NOT DEPLOYED — nothing was broadcast.")

        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:
                return tool._ar(error=(
                    f"refused at gas sizing: {exc} — nothing was broadcast"))
            header += (f"  gas:      limit {tx.get('gas')} "
                       f"(simulation used {decision.sim_gas_used})\n")
        else:
            # Without a measurement the built default (120k) stands, and a
            # deployment costs several times that: it would out-of-gas revert
            # on-chain and burn the whole fee. Refusing is the cheaper failure.
            return tool._ar(error=(
                header + "  RESULT: NOT DEPLOYED — the simulation reported no gas "
                "measurement, so the broadcast gas limit cannot be sized and the "
                "deployment would out-of-gas revert and burn the fee."))

        if dry_run:
            return tool._ar(content=header + (
                "  RESULT: DRY RUN — the guard would allow this, but nothing was "
                "broadcast. Re-run with dry_run=false to deploy."))

        try:
            tx_hash = rail.sign_and_send(tx)
        except Exception as exc:
            return tool._ar(error=f"broadcast failed: {exc} — nothing was deployed")

        _used, _limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb=verb, route=chain, chain=chain,
            amount_in=(facts.predicted_address if facts else chain),
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        receipt = rail.await_receipt(tx_hash)

    # The SETTLED notice is emitted by `_record_spend` below and NOWHERE else
    # (043 T2). It used to fire here, inside the reservation, carrying
    # `ledger_recorded=True` — but A36 deferred the record past the receipt read
    # underneath it, so that flag was a claim about something that had not
    # happened yet and could still raise. `render_settled` prints a loud
    # "⚠ ledger: NOT recorded" on `False`, so a premature `True` is the exact
    # confident-and-wrong shape a money notice may never have.
    def _settled_notice(recorded: bool) -> None:
        try:
            tool._notify_tx(execution_context, tx_notify.TxNotice(
                verb=verb, route=chain, chain=chain,
                amount_in=(facts.predicted_address if facts else chain),
                usd=decision.amount_usd, tx_ref=tx_hash,
                state={"success": tx_notify.STATE_CONFIRMED,
                       "pending": tx_notify.STATE_IN_FLIGHT}.get(
                    receipt.status, tx_notify.STATE_REVERTED),
                detail=f"deployed on {chain}",
                ledger_recorded=recorded), settled=True)
        except Exception:
            # Runs from a `finally` on the money path: a notice that cannot be
            # built must never mask the ledger failure it is reporting.
            logger.debug("deploy: settled notice failed", exc_info=True)

    # Recorded AFTER the landed-vs-predicted check below (043 A36): the address
    # the caller was told before broadcast can be wrong (a nonce race, or no
    # code at all), and the durable spend record must name what actually
    # landed — never the predicted address, and never a chain name in the
    # address field. `counterparty=None` when no receipt address can be read
    # (pending/reverted/no-code) rather than a guess.
    async def _record_spend(counterparty: Optional[str]) -> None:
        # Fix round 1 (043 A36 review): re-acquire the reserve lock for the
        # record alone rather than widening the FIRST reservation across the
        # code/receipt reads above — the address is only known after those
        # reads, so the record is deliberately deferred out of that window.
        # The residual race: a concurrent verb's check() can pass a
        # nearly-exhausted cap during the reads (bounded by one receipt/code
        # read), over-running the cap by at most one transaction <= the
        # per-tx ceiling. A spend is never left unrecorded.
        #
        # The settled notice rides the `finally` so it is emitted exactly once
        # on every path, carrying what the record actually did. A raising
        # record still propagates — an unrecorded spend stays a loud failure —
        # but the owner learns of it in the same notice that reports the
        # transaction, rather than reading a clean line over an invisible spend.
        recorded = False
        try:
            async with gate.reserve():
                gate.record(venue="defi", action=verb,
                            amount_usd=decision.amount_usd or 0.0,
                            counterparty=counterparty,
                            idempotency_key=idem, result_ref=tx_hash, chain=chain)
            recorded = True
        finally:
            _settled_notice(recorded)

    if receipt.succeeded:
        if create2 is not None:
            # A CALL to the factory leaves no `contractAddress` on the receipt,
            # so the confirmation is that CODE now exists at the committed
            # address. The address was already a hash of the init code; this is
            # the observation that it actually landed.
            landed = create2["address"]
            present = _code_present(rail, landed)
            if present is False:
                await _record_spend(None)
                return tool._ar(error=(
                    header + f"  RESULT: the transaction confirmed but there is "
                    f"NO CODE at {landed}. Do not treat this as deployed — check "
                    f"the explorer.\n  tx: {tx_hash}"))
            note = ("" if present else
                    f"  ⚠️ could not read the code back at {landed} — the receipt "
                    f"confirms the transaction, the deployment is unverified.\n")
            await _record_spend(landed)
            return tool._ar(content=header + note + success_note + (
                f"  RESULT: DEPLOYED AND CONFIRMED\n"
                f"  address: {landed}  (same on every chain for these bytes)\n"
                f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
        landed = _landed_address(rail, tx_hash)
        mismatch = ""
        if facts and landed and landed.lower() != facts.predicted_address.lower():
            # Never paper over it: the address the caller was told is the address
            # it will publish, and a nonce race means that address is wrong.
            mismatch = (f"  ⚠️ the contract landed at {landed}, NOT the predicted "
                        f"{facts.predicted_address} — the wallet's nonce moved "
                        f"between building and broadcasting. USE THE LANDED "
                        f"ADDRESS.\n")
        await _record_spend(landed)
        return tool._ar(content=header + mismatch + success_note + (
            f"  RESULT: DEPLOYED AND CONFIRMED\n"
            f"  address: {landed or (facts.predicted_address if facts else 'unknown')}\n"
            f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
    if receipt.status == "pending":
        await _record_spend(None)
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
            f"still land — do NOT retry blindly, a second deploy at a second "
            f"nonce creates a SECOND contract.\n  tx: {tx_hash}"))
    await _record_spend(None)
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the fee was spent, nothing was "
        f"deployed.\n  tx: {tx_hash}"))


def _code_present(rail, address: str):
    """True / False / None — None means the read failed, never "no code".

    A failed read and an empty address look identical if both collapse to
    False, and only one of them means the deployment did not happen.
    """
    try:
        code = rail._rpc("eth_getCode", [address, "latest"])
    except Exception as exc:
        logger.debug("deploy: could not read code at %s: %s", address, exc)
        return None
    if not isinstance(code, str):
        return None
    return len(code) > 2


def _landed_address(rail, tx_hash: str) -> Optional[str]:
    """``contractAddress`` from the receipt — the authoritative answer.

    The prediction is what lets the caller name the address BEFORE broadcast;
    this is what confirms it. A read failure returns None and the caller falls
    back to the prediction with that stated.
    """
    try:
        rec = rail._rpc("eth_getTransactionReceipt", [tx_hash]) or {}
        addr = rec.get("contractAddress")
        if isinstance(addr, str) and addr.startswith("0x"):
            from eth_utils import to_checksum_address
            return to_checksum_address(addr)
    except Exception as exc:
        logger.debug("deploy: could not read contractAddress for %s: %s", tx_hash, exc)
    return None
