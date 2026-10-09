"""``polyrob-signer`` — the service (066 §5).

Two layers:

* :class:`SignerService` — ``handle(request, peer_uid) -> response``. Pure
  enough to test without a socket: every rule the signer enforces lives here.
* :class:`SignerServer` — the Unix socket. It reads the caller's UID from the
  kernel (``SO_PEERCRED`` on Linux, ``LOCAL_PEERCRED`` on macOS) and refuses a
  UID that is neither root nor a configured client before reading a byte.

What the signer enforces, whatever the agent says:

1. **Known shapes only.** An unknown op, field or version is ``unknown_shape``.
   There is no generic message, typed-data or raw-hash signing.
2. **The guard runs here.** ``evm.send`` re-runs
   :func:`core.wallet.tx_guard.authorize` — the same module the agent runs —
   with the signer's own gate (hard caps, its own ledger), its own RPC pins and
   its own pause. There is no second guard implementation.
3. **Hard caps.** ``signer.toml`` per-tx and daily caps bind even when the
   agent's own caps are set above them. Above them: ``approval_required`` with
   an id the owner grants from the CLI ON THE BOX (uid 0, owner decision D5).
4. **Nonces.** A nonce at or below one the signer already signed for, or one the
   chain already consumed, is ``nonce_reuse``; a nonce ahead of the chain is
   ``nonce_gap``.
5. **The holder is the signer's choice.** EVM sends always sign as the
   operational venue key; the agent cannot pick a different key.

069 v4 removed the account binding (the ``account.*`` ops, the ``account_bound`` code and
the ``[account_binding]`` table): an agent acts through an NFT's account only as the NFT's
owner, which ``tx_guard``'s ``via_account`` pre-flight checks — here as in the agent.
"""
import logging
import os
import socket
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from core.signer import protocol
from core.signer.caps import SignerConfig
from core.signer.store import SignerStore

logger = logging.getLogger(__name__)

#: Ops that move money or consume a nonce run one at a time.
_MONEY_OPS = frozenset({
    "evm.send", "x402.authorize", "deposit.sweep", "evm.verdict",
    "venue.sign", "eip8004.feedback_auth", "journal.sign",
})
#: A grant covers the approved amount plus this slack (prices move between the
#: refusal and the retry). Above it the owner is asked again.
_GRANT_SLACK = 1.05
#: EIP-3009 TransferWithAuthorization, exactly (x402 exact scheme).
_EIP3009_FIELDS = [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]
_FEEDBACK_FIELDS = [
    {"name": "agentId", "type": "uint256"}, {"name": "clientAddress", "type": "address"},
    {"name": "expiresAt", "type": "uint256"}, {"name": "nonce", "type": "string"}]
_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}]
#: The signer cannot independently value a Hyperliquid order or reconcile venue
#: exposure yet. Only cancellations are signable, even when the opt-in is armed.
#: Caller-authored prices, sizes and leverage are never a substitute for caps.
HL_ORDER_ACTIONS = frozenset({"cancel", "cancelByCloid", "scheduleCancel"})
_ZERO = "0x0000000000000000000000000000000000000000"
_FEEDBACK_MAX_TTL_SEC = 7 * 86400


class _Refuse(Exception):
    def __init__(self, code: str, reason: str, **extra):
        self.code, self.reason, self.extra = code, reason, extra
        super().__init__(f"{code}: {reason}")


def _is_address(value) -> bool:
    import re
    return isinstance(value, str) and bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", value))


def _addr_eq(a, b) -> bool:
    from core.wallet.addresses import same_address
    return isinstance(a, str) and isinstance(b, str) and same_address(a, b)


class SignerService:
    def __init__(self, config: SignerConfig, wallet, *, store: SignerStore,
                 price_fn: Optional[Callable] = None,
                 fallback_price_fn: Optional[Callable] = None,
                 simulate_fn: Optional[Callable] = None,
                 rpc_is_pinned_fn: Optional[Callable] = None,
                 rail_factory: Optional[Callable] = None,
                 pending_nonce_fn: Optional[Callable] = None,
                 shadow_gate=None,
                 secret_fn: Optional[Callable[[str], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.wallet = wallet
        self.store = store
        self._price_fn = price_fn
        self._fallback_price_fn = fallback_price_fn
        self._simulate_fn = simulate_fn
        self._rpc_is_pinned_fn = rpc_is_pinned_fn
        self._rail_factory = rail_factory
        self._pending_nonce_fn = pending_nonce_fn
        self._secret_fn = secret_fn
        self._now = clock
        self._money_lock = threading.Lock()
        self._started = clock()
        self.gate = wallet.policy
        self.shadow_gate = shadow_gate if shadow_gate is not None else self._make_shadow_gate()

    def _make_shadow_gate(self):
        """The gate shadow verdicts are booked against: the same hard caps over
        a SEPARATE ledger, so a week of shadow shows where the daily cap would
        have bound without charging the real one."""
        from core.wallet.audit_sink import JsonlAuditSink
        from core.wallet.policy import PolicyGate
        path = os.path.join(self.config.state_dir, "shadow", "audit.jsonl")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        return PolicyGate(max_per_tx_usd=self.config.per_tx_usd,
                          audit_sink=JsonlAuditSink(path),
                          daily_cap_usd=self.config.daily_usd)

    # -- entry -------------------------------------------------------------

    def peer_allowed(self, peer_uid: int) -> bool:
        return peer_uid == 0 or peer_uid in self.config.client_uids

    def handle(self, obj: Dict[str, Any], peer_uid: int) -> Dict[str, Any]:
        if not self.peer_allowed(peer_uid):
            return protocol.refusal(protocol.PEER_REFUSED,
                                    f"uid {peer_uid} is not a signer client")
        try:
            op, body = protocol.parse_request(obj)
        except protocol.ProtocolError as exc:
            return protocol.refusal(protocol.UNKNOWN_SHAPE, str(exc))
        if op in protocol.ROOT_ONLY_OPS and peer_uid != 0:
            self.store.log_decision(op=op, digest=None, allowed=False,
                                    code=protocol.NOT_ROOT, peer_uid=peer_uid)
            return protocol.refusal(protocol.NOT_ROOT, (
                f"{op} is the owner's, from the CLI on the box (as root); uid "
                f"{peer_uid} may not call it. A Telegram approval runs inside the "
                f"agent process and cannot unlock anything above the signer's hard cap."))
        handler = getattr(self, "_op_" + op.replace(".", "_"))
        try:
            # Pause changes share the signing lock: once pause.set answers,
            # every later signing request observes the acknowledged pause.
            if op in _MONEY_OPS or op == "pause.set":
                with self._money_lock:
                    if op in _MONEY_OPS and self.store.paused():
                        raise _Refuse(protocol.PAUSED,
                                      "the signer's spend pause is ON (owner, on the box)")
                    return protocol.ok(handler(body, peer_uid))
            return protocol.ok(handler(body, peer_uid))
        except _Refuse as r:
            return protocol.refusal(r.code, r.reason, **r.extra)
        except protocol.ProtocolError as exc:
            return protocol.refusal(protocol.UNKNOWN_SHAPE, str(exc))
        except Exception as exc:  # noqa: BLE001 — a signer bug refuses, never signs
            logger.exception("signer: %s failed", op)
            return protocol.refusal(protocol.INTERNAL, f"{op} failed: {type(exc).__name__}")

    # -- read ops ----------------------------------------------------------

    def _op_ping(self, body, peer_uid):
        protocol.check_keys(body)
        return {"caps": self.config.summary(), "paused": self.store.paused(),
                "uptime_sec": round(self._now() - self._started, 1),
                "pending_approvals": len(self.store.pending()),
                "schema": protocol.SCHEMA_VERSION}

    def identity(self) -> Dict[str, Any]:
        from core.wallet.agent_wallet import VENUES
        evm = {v: self.wallet.signer_for(v).address for v in sorted(VENUES)}
        out = {"evm": evm, "scheme": self.wallet.scheme,
               "network": self.config.network,
               "operational_venue": self.config.operational_venue}
        try:
            out["solana"] = self.wallet.solana_address
        except Exception:  # the solana extra may be absent; EVM is still whole
            out["solana"] = None
        return out

    def _op_identity(self, body, peer_uid):
        protocol.check_keys(body)
        return self.identity()

    def _op_approvals_list(self, body, peer_uid):
        protocol.check_keys(body)
        return {"pending": [
            {k: row[k] for k in ("id", "op", "chain", "amount_usd", "summary", "created", "expires")}
            for row in self.store.pending()]}

    # -- owner ops (uid 0) -------------------------------------------------

    def _op_approvals_decide(self, body, peer_uid):
        protocol.check_keys(body, required=("id", "grant"))
        if not isinstance(body["grant"], bool):
            raise protocol.ProtocolError("grant must be true or false")
        row = self.store.decide(str(body["id"]), grant=body["grant"], uid=peer_uid,
                                ttl_sec=self.config.approval_ttl_sec)
        if row is None:
            raise _Refuse(protocol.UNKNOWN_SHAPE, f"no pending signer approval {body['id']!r}")
        self.store.log_decision(op="approvals.decide", digest=row["digest"], allowed=body["grant"],
                                peer_uid=peer_uid, amount_usd=row["amount_usd"], ref=row["id"])
        return {"id": row["id"], "state": row["state"], "amount_usd": row["amount_usd"]}

    def _op_pause_set(self, body, peer_uid):
        protocol.check_keys(body, required=("paused",))
        if not isinstance(body["paused"], bool):
            raise protocol.ProtocolError("paused must be true or false")
        self.store.set_paused(body["paused"])
        self.store.log_decision(op="pause.set", digest=None, allowed=True, peer_uid=peer_uid,
                                reason="paused" if body["paused"] else "resumed")
        return {"paused": body["paused"]}

    # -- EVM ---------------------------------------------------------------

    def _chain_row(self, chain: str):
        from core.wallet import chains
        if chain not in self.config.chains:
            raise _Refuse(protocol.CHAIN_NOT_ALLOWED,
                          f"chain {chain!r} is not in signer.toml [policy] chains "
                          f"{list(self.config.chains)}")
        row = chains.get(chain)
        if row is None or not row.money_enabled:
            raise _Refuse(protocol.CHAIN_NOT_ALLOWED, f"chain {chain!r} is not money-enabled")
        return row

    def _authorize(self, intent, tx, *, gate):
        from core.wallet import tx_guard
        return tx_guard.authorize(
            intent, tx, holder=self.wallet.operational_signer().address, gate=gate,
            execution_context=None, tool_self=None,
            simulate_fn=self._simulate_fn, price_fn=self._price_fn,
            fallback_price_fn=self._fallback_price_fn,
            rpc_is_pinned_fn=self._rpc_is_pinned_fn,
            halted_fn=self.store.paused, entry_paused_fn=lambda: False)

    @staticmethod
    def _passes(decision) -> bool:
        # Structural guard success, possibly requiring independent approval.
        # This alone is not authority to send an owner_queue decision.
        return bool(decision.allowed) or decision.lane == "owner_queue"

    @staticmethod
    def _cap_refusal(decision) -> bool:
        # The structured flag the PolicyGate sets on a cap refusal — never the reason
        # text, which a refusal of another kind could be made to contain.
        return decision.amount_usd is not None and bool(getattr(decision, "cap_exceeded", False))

    def _evaluate(self, body, *, gate, open_approval: bool):
        """``(intent, tx, decision, grant_row)`` or :class:`_Refuse`."""
        protocol.check_keys(body, required=("intent", "tx"))
        intent = protocol.intent_from_wire(body["intent"])
        tx = protocol.tx_from_wire(body["tx"])
        row = self._chain_row(intent.chain)
        if int(tx["chainId"]) != int(row.chain_id):
            raise _Refuse(protocol.CHAIN_NOT_ALLOWED,
                          f"transaction chainId {tx['chainId']} is not {intent.chain} ({row.chain_id})")
        from core.wallet.broadcast.evm import MAX_GAS_LIMIT
        fee = int(tx["gas"]) * int(tx["maxFeePerGas"])
        if int(tx["gas"]) > MAX_GAS_LIMIT or fee > int(row.max_fee_wei_per_tx):
            raise _Refuse(protocol.FEE_CEILING,
                          f"gas {tx['gas']} x maxFeePerGas {tx['maxFeePerGas']} = {fee} wei is "
                          f"above the {intent.chain} ceiling {row.max_fee_wei_per_tx} wei")
        if self.store.paused():
            raise _Refuse(protocol.PAUSED, "the signer's spend pause is ON (owner, on the box)")
        decision = self._authorize(intent, tx, gate=gate)
        # Fee-only valuation cannot bound the principal of an NFT or LP exit.
        # An app-side approval stamp is not an independent signer approval.
        unpriced_assets = bool(intent.is_nft_op or (
            intent.is_liquidity_op and not intent.lp_outflows))
        needs_review = unpriced_assets or decision.lane == "owner_queue"
        if decision.allowed and not needs_review:
            return intent, tx, decision, None
        digest = protocol.request_digest(body["intent"], tx)
        if not self._cap_refusal(decision) and not (needs_review and self._passes(decision)):
            raise _Refuse(protocol.GUARD_REFUSED, decision.reason,
                          amount_usd=decision.amount_usd, digest=digest)
        # Only the owner's independent on-box grant unlocks this request.
        existing = self.store.find_approval(digest)
        amount = float(decision.amount_usd)
        if (existing is not None and existing["state"] == "granted"
                and amount <= float(existing["amount_usd"]) * _GRANT_SLACK):
            from core.wallet.policy import PolicyGate
            granted_gate = PolicyGate(max_per_tx_usd=max(amount, self.config.per_tx_usd),
                                      audit_sink=gate._audit, daily_cap_usd=None)
            regranted = self._authorize(intent, tx, gate=granted_gate)
            if self._passes(regranted):
                return intent, tx, regranted, existing
            raise _Refuse(protocol.GUARD_REFUSED, regranted.reason, digest=digest)
        if not open_approval:
            raise _Refuse(protocol.APPROVAL_REQUIRED, decision.reason,
                          amount_usd=amount, digest=digest)
        from core.signer.review import evm_review
        summary = evm_review(intent, tx, decision, digest=digest, unpriced_assets=unpriced_assets)
        row_ = self.store.open_approval(digest=digest, op="evm.send", chain=intent.chain,
                                        amount_usd=amount, summary=summary,
                                        ttl_sec=self.config.approval_ttl_sec)
        raise _Refuse(protocol.APPROVAL_REQUIRED, (
            f"${amount:.2f} requires independent signer approval ({decision.reason}). The owner "
            f"approves it ON THE BOX: `sudo polyrob owner promote signer_approval {row_['id']}`, "
            f"then the same request is retried. Nothing was signed."),
            amount_usd=amount, approval_id=row_["id"])

    def _op_evm_verdict(self, body, peer_uid):
        """Shadow: the verdict ``evm.send`` would reach, never a signature.
        An allowed verdict is booked in the SHADOW ledger (not the real one)."""
        try:
            intent, tx, decision, _grant = self._evaluate(body, gate=self.shadow_gate,
                                                          open_approval=False)
        except _Refuse as r:
            self.store.log_decision(op="evm.verdict", digest=r.extra.get("digest"), allowed=False,
                                    code=r.code, reason=r.reason, peer_uid=peer_uid,
                                    amount_usd=r.extra.get("amount_usd"))
            raise
        nonce_note = self._nonce_problem(intent.chain, int(tx["nonce"]), check_chain=False)
        if nonce_note:
            self.store.log_decision(op="evm.verdict", digest=None, allowed=False,
                                    code=protocol.NONCE_REUSE, reason=nonce_note, peer_uid=peer_uid)
            raise _Refuse(protocol.NONCE_REUSE, nonce_note)
        digest = protocol.request_digest(body["intent"], tx)
        try:
            self.shadow_gate.record(venue="defi", action="shadow_verdict",
                                    amount_usd=decision.amount_usd or 0.0,
                                    counterparty=intent.to, idempotency_key=intent.idempotency_key,
                                    result_ref="shadow:" + digest[:16], chain=intent.chain)
        except Exception:
            logger.warning("shadow ledger write failed", exc_info=True)
        self.store.log_decision(op="evm.verdict", digest=digest, allowed=True, peer_uid=peer_uid,
                                amount_usd=decision.amount_usd, reason=decision.reason)
        return {"allowed": True, "reason": decision.reason, "lane": decision.lane,
                "amount_usd": decision.amount_usd}

    def _nonce_problem(self, chain: str, nonce: int, *, check_chain: bool) -> Optional[str]:
        holder = self.wallet.operational_signer().address
        used = self.store.highest_nonce(chain, holder)
        if used is not None and nonce <= used:
            return (f"nonce {nonce} on {chain} was already signed by the signer "
                    f"(highest used {used}); a second transaction at it is refused")
        if not check_chain:
            return None
        onchain = self._pending_nonce(chain, holder)
        if onchain is None:
            raise _Refuse(protocol.INTERNAL, f"could not read the {chain} nonce of {holder}")
        if nonce < onchain:
            return f"nonce {nonce} on {chain} is already consumed on chain (next is {onchain})"
        if nonce > onchain:
            raise _Refuse(protocol.NONCE_GAP,
                          f"nonce {nonce} on {chain} is ahead of the chain (next is {onchain}); "
                          f"it would park in the mempool")
        return None

    def _pending_nonce(self, chain: str, holder: str) -> Optional[int]:
        if self._pending_nonce_fn is not None:
            return self._pending_nonce_fn(chain, holder)
        from core.wallet.broadcast import evm
        raw = evm._rpc_call(chain, "eth_getTransactionCount", [holder, "pending"])
        return evm._hex_to_int(raw)

    def _rail(self, chain: str, signer, **kw):
        if self._rail_factory is not None:
            return self._rail_factory(chain=chain, signer=signer, **kw)
        from core.wallet.broadcast.evm import EvmRail
        return EvmRail(chain=chain, signer=signer, **kw)

    def _op_evm_send(self, body, peer_uid):
        try:
            intent, tx, decision, grant = self._evaluate(body, gate=self.gate, open_approval=True)
        except _Refuse as r:
            self.store.log_decision(op="evm.send", digest=r.extra.get("digest"), allowed=False,
                                    code=r.code, reason=r.reason, peer_uid=peer_uid,
                                    amount_usd=r.extra.get("amount_usd"),
                                    ref=r.extra.get("approval_id"))
            raise
        chain, nonce = intent.chain, int(tx["nonce"])
        signer = self.wallet.operational_signer()
        problem = self._nonce_problem(chain, nonce, check_chain=True)
        if problem or not self.store.reserve_nonce(chain, signer.address, nonce):
            reason = problem or f"nonce {nonce} on {chain} was just claimed by another request"
            self.store.log_decision(op="evm.send", digest=None, allowed=False,
                                    code=protocol.NONCE_REUSE, reason=reason, peer_uid=peer_uid)
            raise _Refuse(protocol.NONCE_REUSE, reason)
        from core.wallet.broadcast.evm import BroadcastError
        rail = self._rail(chain, signer)
        try:
            tx_hash = rail.sign_and_send(tx)
        except BroadcastError as exc:
            # Not sent (preflight refused, or the node REFUSED the bytes): the
            # nonce is free again.
            self.store.release_nonce(chain, signer.address, nonce)
            self.store.log_decision(op="evm.send", digest=None, allowed=False,
                                    code=protocol.BROADCAST_FAILED, reason=str(exc), peer_uid=peer_uid)
            raise _Refuse(protocol.BROADCAST_FAILED, f"not sent: {exc}")
        except Exception as exc:
            # Unknown outcome: the nonce stays claimed and the signer's journal
            # interlock stands until reconciled on the box. It may have left, so
            # an owner grant is spent: a retry needs a new approval.
            if grant is not None:
                self.store.consume_grant(grant["id"])
            logger.exception("signer: send outcome unknown")
            raise _Refuse(protocol.INTERNAL, (
                f"send outcome UNKNOWN ({type(exc).__name__}); nonce {nonce} stays claimed "
                f"and the signer's journal holds until reconciled on the box"))
        # Spend the grant BEFORE booking: a ledger write that raises must not
        # leave a one-use owner approval live for a second send.
        if grant is not None:
            self.store.consume_grant(grant["id"])
        self.store.bind_nonce(chain, signer.address, nonce, tx_hash)
        self.gate.record(venue="defi", action="signer_evm_send",
                         amount_usd=decision.amount_usd or 0.0, counterparty=intent.to,
                         idempotency_key=intent.idempotency_key, result_ref=tx_hash, chain=chain)
        self.store.log_decision(op="evm.send", digest=protocol.request_digest(body["intent"], tx),
                                allowed=True, peer_uid=peer_uid, amount_usd=decision.amount_usd,
                                ref=tx_hash, reason=decision.reason)
        return {"tx_hash": tx_hash, "amount_usd": decision.amount_usd, "lane": decision.lane,
                "reason": decision.reason, "sim_gas_used": decision.sim_gas_used}

    # -- x402 (EIP-3009) ---------------------------------------------------

    def _usdc_chain_for(self, chain_id: int, contract: str):
        from core.wallet import chains
        for name in self.config.chains:
            row = chains.get(name)
            if row is not None and int(row.chain_id) == int(chain_id):
                if row.usdc and _addr_eq(row.usdc, contract):
                    return row
                raise _Refuse(protocol.UNKNOWN_SHAPE,
                              f"verifyingContract {contract} is not the pinned USDC on {name}")
        raise _Refuse(protocol.CHAIN_NOT_ALLOWED, f"chainId {chain_id} is not a signer chain")

    @staticmethod
    def _types_without_domain(types: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in types.items() if k != "EIP712Domain"}

    def _op_x402_authorize(self, body, peer_uid):
        protocol.check_keys(body, required=("domain", "types", "primary_type", "message"))
        domain, types, message = body["domain"], body["types"], body["message"]
        if body["primary_type"] != "TransferWithAuthorization":
            raise protocol.ProtocolError("x402.authorize signs TransferWithAuthorization only")
        if not isinstance(types, dict) or self._types_without_domain(types) != {
                "TransferWithAuthorization": _EIP3009_FIELDS}:
            raise protocol.ProtocolError("types are not exactly EIP-3009 TransferWithAuthorization")
        if "EIP712Domain" in types and types["EIP712Domain"] != _DOMAIN_FIELDS:
            raise protocol.ProtocolError("EIP712Domain is not name/version/chainId/verifyingContract")
        protocol.check_keys(domain, required=("name", "version", "chainId", "verifyingContract"))
        if domain["name"] not in ("USD Coin", "USDC"):
            raise protocol.ProtocolError(f"domain name {domain['name']!r} is not USDC")
        row = self._usdc_chain_for(int(domain["chainId"]), str(domain["verifyingContract"]))
        protocol.check_keys(message, required=("from", "to", "value", "validAfter",
                                               "validBefore", "nonce"))
        signer = self.wallet.operational_signer()
        if not _addr_eq(message["from"], signer.address):
            raise _Refuse(protocol.UNKNOWN_SHAPE,
                          f"from {message['from']} is not the operational address {signer.address}")
        value = int(message["value"])
        now = self._now()
        valid_after, valid_before = int(message["validAfter"]), int(message["validBefore"])
        if value <= 0 or valid_after > now + 5 or valid_before <= now \
                or valid_before - now > self.config.x402_max_window_sec:
            raise _Refuse(protocol.UNKNOWN_SHAPE, (
                f"authorization window {valid_after}..{valid_before} is outside "
                f"now..now+{self.config.x402_max_window_sec}s, or the value is not positive"))
        nonce = str(message["nonce"])
        if not (nonce.startswith("0x") and len(nonce) == 66):
            raise protocol.ProtocolError("nonce must be a 32-byte 0x hex string")
        usd = round(value / 10 ** 6, 6)
        if usd > self.config.x402_per_payment_usd:
            raise _Refuse(protocol.APPROVAL_REQUIRED, (
                f"x402 payment ${usd:.2f} is above the signer's per-payment cap "
                f"${self.config.x402_per_payment_usd:.2f}"), amount_usd=usd)
        key = "x402:" + nonce.lower()
        verdict = self.gate.check(venue="x402", amount_usd=usd, idempotency_key=key)
        if not verdict.allowed:
            self.store.log_decision(op="x402.authorize", digest=key, allowed=False,
                                    code=protocol.GUARD_REFUSED, reason=verdict.reason,
                                    peer_uid=peer_uid, amount_usd=usd)
            raise _Refuse(protocol.GUARD_REFUSED, f"refused by the signer's gate: {verdict.reason}",
                          amount_usd=usd)
        msg = dict(message, value=value, validAfter=valid_after, validBefore=valid_before,
                   nonce=bytes.fromhex(nonce[2:]))
        signed = signer.account.sign_typed_data(
            domain_data=dict(domain), message_types=self._types_without_domain(types),
            message_data=msg)
        sig = bytes(signed.signature).hex()
        self.gate.record(venue="x402", action="signer_x402_authorize", amount_usd=usd,
                         counterparty=str(message["to"]), idempotency_key=key,
                         result_ref=key, chain=row.name)
        self.store.log_decision(op="x402.authorize", digest=key, allowed=True, peer_uid=peer_uid,
                                amount_usd=usd, ref=str(message["to"]))
        return {"signature": "0x" + sig, "amount_usd": usd}

    # -- 066 P3: Hyperliquid orders ------------------------------------------

    def _op_venue_sign(self, body, peer_uid):
        protocol.check_keys(body, required=("venue", "action", "nonce", "is_mainnet"),
                            optional=("expires_after", "vault_address"))
        if body["venue"] != "hyperliquid":
            raise protocol.ProtocolError(f"venue {body['venue']!r} is not signable here")
        if not self.config.hyperliquid_orders:
            raise _Refuse(protocol.NOT_CONFIGURED,
                          "signer.toml [policy] hyperliquid_orders is false")
        if body.get("vault_address"):
            raise _Refuse(protocol.UNKNOWN_SHAPE, "vault actions are not signed by the signer")
        action = body["action"]
        if not isinstance(action, dict) or action.get("type") not in HL_ORDER_ACTIONS:
            kind = action.get("type") if isinstance(action, dict) else None
            raise _Refuse(protocol.UNKNOWN_SHAPE, (
                f"Hyperliquid action {kind!r} lacks independent signer valuation and caps; "
                f"only cancellation actions {sorted(HL_ORDER_ACTIONS)} are signable"))
        is_mainnet = body["is_mainnet"]
        if not isinstance(is_mainnet, bool) or is_mainnet != (self.config.network == "mainnet"):
            raise _Refuse(protocol.UNKNOWN_SHAPE,
                          f"is_mainnet={is_mainnet!r} does not match the signer network "
                          f"{self.config.network!r}")
        nonce = body["nonce"]
        expires = body.get("expires_after")
        if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= 0 or \
                (expires is not None and (isinstance(expires, bool) or not isinstance(expires, int))):
            raise protocol.ProtocolError("nonce/expires_after must be integers")
        try:
            from hyperliquid.utils.signing import sign_l1_action
        except Exception as exc:
            raise _Refuse(protocol.NOT_CONFIGURED,
                          f"the Hyperliquid SDK is not importable in the signer ({type(exc).__name__})")
        hl_signer = self.wallet.signer_for("hyperliquid")
        account = hl_signer.account
        sig = sign_l1_action(account, action, None, nonce, expires, is_mainnet)
        self.store.log_decision(op="venue.sign", digest=None, allowed=True, peer_uid=peer_uid,
                                reason=f"hyperliquid {action.get('type')}")
        return {"signature": sig}

    # -- C1: the account journal entry ---------------------------------------

    def _op_journal_sign(self, body, peer_uid):
        """EIP-191 over EXACTLY one account journal template (``account_journal``) by the
        operational EVM key — the NFT owner that signs the entry. Any other bytes refuse: the
        owner key is an ERC-1271 signer of every account it owns, so this op signs nothing else."""
        protocol.check_keys(body, required=("message",))
        from core.wallet.account_journal import is_journal_template
        message = body["message"]
        data = message.encode("utf-8") if isinstance(message, str) else b""
        if not is_journal_template(data):
            raise _Refuse(protocol.UNKNOWN_SHAPE, "the message is not the account journal template")
        signer = self.wallet.operational_signer()
        sig = signer.sign_message(data)
        account = message.split("\n")[1].split("=", 1)[1].lower()
        self.store.log_decision(op="journal.sign", digest=None, allowed=True, peer_uid=peer_uid,
                                reason=f"journal {account}")
        return {"signature": sig, "address": signer.address}

    # -- 066 P3: EIP-8004 feedback authorization ---------------------------

    def _op_eip8004_feedback_auth(self, body, peer_uid):
        protocol.check_keys(body, required=("typed",))
        typed = body["typed"]
        protocol.check_keys(typed, required=("types", "primaryType", "domain", "message"))
        if typed["primaryType"] != "FeedbackAuth" or typed["types"] != {
                "EIP712Domain": _DOMAIN_FIELDS, "FeedbackAuth": _FEEDBACK_FIELDS}:
            raise protocol.ProtocolError("typed data is not the EIP-8004 FeedbackAuth shape")
        protocol.check_keys(typed["domain"], required=("name", "version", "chainId",
                                                       "verifyingContract"))
        if typed["domain"]["name"] != "EIP8004ReputationRegistry" or \
                typed["domain"]["version"] != "1":
            raise protocol.ProtocolError("domain is not EIP8004ReputationRegistry v1")
        # The domain must name a PINNED reputation registry on its own chain, or the
        # signature authorizes feedback on whatever contract/chain the caller chose.
        from core.wallet import erc8004
        try:
            _chain_id = int(typed["domain"]["chainId"])
        except (TypeError, ValueError):
            raise protocol.ProtocolError("domain chainId is not an integer")
        _verifier = str(typed["domain"]["verifyingContract"]).lower()
        # The zero address (an unconfigured registry) verifies nowhere, so it is inert.
        if _verifier != "0x" + "0" * 40 and not any(
                row.chain_id == _chain_id and str(row.reputation).lower() == _verifier
                for row in (erc8004.registry_for(c) for c in erc8004.supported_chains())):
            raise _Refuse(protocol.UNKNOWN_SHAPE,
                          "domain is not a pinned ERC-8004 reputation registry on its chain")
        msg = typed["message"]
        protocol.check_keys(msg, required=("agentId", "clientAddress", "expiresAt", "nonce"))
        expires = int(msg["expiresAt"])
        if not (self._now() < expires <= self._now() + _FEEDBACK_MAX_TTL_SEC):
            raise _Refuse(protocol.UNKNOWN_SHAPE,
                          f"expiresAt must be within {_FEEDBACK_MAX_TTL_SEC // 86400} days")
        key = (self._secret_fn or (lambda n: None))("EIP8004_AGENT_PRIVATE_KEY")
        if not key:
            raise _Refuse(protocol.NOT_CONFIGURED,
                          "EIP8004_AGENT_PRIVATE_KEY is not in the signer's env")
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        signed = Account.sign_message(encode_typed_data(full_message=typed), key)
        sig = signed.signature.hex()
        self.store.log_decision(op="eip8004.feedback_auth", digest=None, allowed=True,
                                peer_uid=peer_uid, ref=str(msg["clientAddress"]))
        return {"signature": sig if sig.startswith("0x") else "0x" + sig}

    # -- deposits + sweeper (066 §5.5) --------------------------------------

    def _deposit_seed(self) -> str:
        fn = self._secret_fn or (lambda n: None)
        seed = fn("PAYMENT_MASTER_SEED") or fn("MASTER_SEED")
        if not seed or len(seed) < 32:
            raise _Refuse(protocol.NOT_CONFIGURED,
                          "no deposit master seed (PAYMENT_MASTER_SEED/MASTER_SEED) in the signer")
        return seed

    def _deposit_account(self, user_id: str):
        from core.wallet.derivation import derive_deposit_key
        from core.wallet.signer import LocalEoaSigner
        if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
            raise protocol.ProtocolError("user_id must be a non-empty string")
        return LocalEoaSigner(derive_deposit_key(self._deposit_seed(), user_id))

    def _op_deposit_address(self, body, peer_uid):
        protocol.check_keys(body, required=("user_id",))
        return {"address": self._deposit_account(body["user_id"]).address}

    def _op_deposit_sweep(self, body, peer_uid):
        """Sweep ONE deposit to the PINNED destination. The agent names the
        deposit; it cannot name where the funds go, or how much stays behind."""
        protocol.check_keys(body, required=("user_id", "chain", "token_symbol", "deposit_address"))
        dest = self.config.sweep_destination
        if not dest:
            raise _Refuse(protocol.NOT_CONFIGURED, "signer.toml has no [sweep] destination")
        chain = str(body["chain"])
        self._chain_row(chain)
        signer = self._deposit_account(body["user_id"])
        if not _addr_eq(signer.address, body["deposit_address"]):
            raise _Refuse(protocol.UNKNOWN_SHAPE, "deposit address does not match the derived key")
        symbol = str(body["token_symbol"]).upper()
        from core.wallet.broadcast import evm
        if symbol == "ETH":
            rail = self._rail(chain, signer, gas_limit=21_000)
            balance = evm._hex_to_int(rail._rpc("eth_getBalance", [signer.address, "latest"])) or 0
            probe = rail.build_native_transfer(to=dest, amount_wei=1)
            amount = balance - int(probe["gas"]) * int(probe["maxFeePerGas"])
            if amount <= 0:
                raise _Refuse(protocol.GUARD_REFUSED, "balance does not cover the sweep fee")
            tx = dict(probe, value=int(amount))
        else:
            token = self.config.sweep_tokens.get(f"{chain}:{symbol}")
            if not token:
                raise _Refuse(protocol.NOT_CONFIGURED, f"no pinned {symbol} on {chain} in [sweep] tokens")
            rail = self._rail(chain, signer, gas_limit=100_000)
            data = "0x70a08231" + signer.address[2:].lower().rjust(64, "0")
            balance = evm._hex_to_int(rail._rpc("eth_call", [{"to": token, "data": data}, "latest"])) or 0
            if balance <= 0:
                raise _Refuse(protocol.GUARD_REFUSED, f"no {symbol} balance to sweep")
            tx = rail.build_erc20_transfer(token=token, to=dest, amount_raw=balance)
        nonce = int(tx["nonce"])
        if not self.store.reserve_nonce(chain, signer.address, nonce):
            raise _Refuse(protocol.NONCE_REUSE, f"deposit nonce {nonce} already signed")
        from core.wallet.broadcast.evm import BroadcastError
        try:
            tx_hash = rail.sign_and_send(tx)
        except BroadcastError as exc:
            self.store.release_nonce(chain, signer.address, nonce)
            raise _Refuse(protocol.BROADCAST_FAILED, f"not sent: {exc}")
        self.store.bind_nonce(chain, signer.address, nonce, tx_hash)
        # A sweep is not a treasury SPEND (the funds move INTO the treasury);
        # it never charges the caps, but it releases its journal row here.
        from core.wallet import submission_journal
        submission_journal.mark_booked(tx_hash)
        self.store.log_decision(op="deposit.sweep", digest=None, allowed=True, peer_uid=peer_uid,
                                ref=tx_hash, reason=f"{symbol} on {chain} to {dest}")
        return {"tx_hash": tx_hash, "destination": dest}


# -- socket ------------------------------------------------------------------

def peer_pid_of(conn) -> Optional[int]:
    """The connecting process's PID (Linux ``SO_PEERCRED``); None when unknown."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", creds)
    except (OSError, struct.error, TypeError):
        return None
    return int(pid) if pid > 0 else None


def peer_uid_of(conn) -> int:
    """The connecting process's UID, from the kernel. Raises when unknown."""
    if hasattr(socket, "SO_PEERCRED"):
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return int(uid)
    if sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):
        # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
        raw = conn.getsockopt(0, socket.LOCAL_PEERCRED, 4 + 4 + 2 + 2 + 16 * 4)
        _version, uid = struct.unpack_from("<Ii", raw, 0)
        return int(uid)
    raise OSError("no peer-credential support on this platform; refusing every connection")


class SignerServer:
    def __init__(self, service: SignerService, path: str, *, group: Optional[str] = None,
                 conn_timeout: float = 120.0):
        self.service = service
        self.path = str(path)
        self.group = group
        self.conn_timeout = conn_timeout
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._connections = threading.BoundedSemaphore(16)
        self._peer_warned: set = set()

    def peer_process_allowed(self, conn, uid: int) -> bool:
        """WAL-1: only the main process of a client UID (core/signer/peer.py).

        Root is the owner and is never checked. ``peer_check = "uid"`` keeps the
        UID check alone; ``"warn"`` logs a non-main peer and serves it;
        ``"main_process"`` refuses it. "Cannot tell" (no SO_PEERCRED pid, no
        cgroup v2, a hidden /proc) falls back to the UID check with a warning.
        """
        mode = getattr(self.service.config, "peer_check", "warn")
        if uid == 0 or mode == "uid":
            return True
        from core.signer.peer import is_main_process
        pid = peer_pid_of(conn)
        verdict = is_main_process(pid, uid) if pid else None
        if verdict is None:
            self._warn_once(("unknown", uid),
                            "signer: cannot tell whether uid %s pid %s is a client's main "
                            "process (no cgroup v2 / hidden /proc?); falling back to the UID "
                            "check", uid, pid)
            return True
        if verdict:
            return True
        if mode == "main_process":
            logger.warning("signer: refused uid %s pid %s — not the client's main process "
                           "(a child of the agent)", uid, pid)
            return False
        self._warn_once(("child", pid),
                        "signer: uid %s pid %s is not the client's main process (a child of "
                        "the agent); served because peer_check = \"warn\" — set "
                        "peer_check = \"main_process\" in signer.toml to refuse it", uid, pid)
        return True

    def _warn_once(self, key, msg, *args) -> None:
        if key in self._peer_warned:
            return
        if len(self._peer_warned) < 1024:
            self._peer_warned.add(key)
        logger.warning(msg, *args)

    def bind(self) -> None:
        import stat
        if os.path.lexists(self.path):
            if not stat.S_ISSOCK(os.lstat(self.path).st_mode):
                raise RuntimeError(f"{self.path} exists and is not a socket; refusing to replace it")
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o660)
        if self.group:
            import grp
            os.chown(self.path, -1, grp.getgrnam(self.group).gr_gid)
        sock.listen(16)
        sock.settimeout(1.0)
        self._sock = sock

    def serve_forever(self) -> None:
        if self._sock is None:
            self.bind()
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if not self._connections.acquire(blocking=False):
                conn.close()
                continue
            try:
                threading.Thread(target=self._serve_bounded, args=(conn,), daemon=True).start()
            except RuntimeError:
                conn.close()
                self._connections.release()
                logger.error("signer: connection worker unavailable")

    def _serve_bounded(self, conn) -> None:
        try:
            self._serve_conn(conn)
        finally:
            self._connections.release()

    def _serve_conn(self, conn) -> None:
        with conn:
            conn.settimeout(self.conn_timeout)
            try:
                uid = peer_uid_of(conn)
            except OSError as exc:
                logger.error("signer: peer credentials unreadable (%s); closing", exc)
                return
            if not self.service.peer_allowed(uid):
                # Refused before a byte of the request is read.
                logger.warning("signer: refused a connection from uid %s", uid)
                try:
                    protocol.send_frame(conn, protocol.refusal(
                        protocol.PEER_REFUSED, f"uid {uid} is not a signer client"))
                except OSError:
                    pass
                return
            if not self.peer_process_allowed(conn, uid):
                try:
                    protocol.send_frame(conn, protocol.refusal(
                        protocol.PEER_REFUSED,
                        "only the client's main process may use the signer, not a child of it"))
                except OSError:
                    pass
                return
            try:
                obj = protocol.recv_frame(conn)
            except (protocol.ProtocolError, ConnectionError, OSError) as exc:
                try:
                    protocol.send_frame(conn, protocol.refusal(protocol.UNKNOWN_SHAPE, str(exc)))
                except OSError:
                    pass
                return
            response = self.service.handle(obj, uid)
            try:
                protocol.send_frame(conn, response)
            except OSError:
                logger.warning("signer: could not deliver the response for %s", obj.get("op"))

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass
