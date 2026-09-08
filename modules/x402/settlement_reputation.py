"""ERC-8004 payment-backed feedback OFFER on a correspondent-linked settlement (never auto-submits).

Split out of ``modules/x402/settlement_watcher.py`` (S6, 2026-08-29). A mixin over
``SettlementWatcher`` — it relies on the host for ``task_agent``, ``_db``, the injection
seams (``_rpc_call``/``_usdc_addr``/``_goal_board_override``/``_reputation_manager_override``)
and the module ``logger``; behaviour is byte-identical to the pre-split class.
"""
import logging


logger = logging.getLogger("modules.x402.settlement_watcher")

class SettlementReputationMixin:
    def _reputation_mgr(self):
        """The ERC-8004 `ReputationManager` the payment-feedback-authorization
        hook uses. An injected test double (constructor `reputation_manager=`)
        wins; production lazily builds a real one sharing this watcher's
        `db` — mirrors `_goal_board()`'s lazy-build pattern."""
        if self._reputation_manager_override is not None:
            return self._reputation_manager_override
        from modules.eip8004.reputation import ReputationManager
        return ReputationManager(db=self._db)
    async def _maybe_offer_payment_feedback(
        self, inv: dict, session_id: str, cref: dict,
    ) -> None:
        """Task 15 (Phase 4): offer the payer a signed ERC-8004 feedback
        AUTHORIZATION + payment proof once their invoice settles — the
        anti-sybil "verified paying customer" signal ERC-8004 reputation was
        designed for. This method NEVER submits feedback on the payer's
        behalf (that would be fabricated reputation) — it only creates the
        redeemable authorization and, best-effort, tells the payer it exists.

        Gated `EIP8004_PAYMENT_FEEDBACK` (rides `EIP8004_ENABLED` — see
        `core.config_policy.eip8004_payment_feedback_enabled`). Requires a
        verifiable on-chain transaction hash: a settlement with none (e.g. a
        manually attested ``settled_no_tx`` invoice) has nothing to prove, so
        is silently skipped rather than offering a hollow proof. The caller
        (`_notify`) already guarantees an identifiable payer (an ACTIVE
        correspondent channel) and wraps this whole call fail-open.
        """
        from core.config_policy import eip8004_payment_feedback_enabled
        if not eip8004_payment_feedback_enabled():
            return
        tx_hash = inv.get("transaction_hash")
        if not tx_hash:
            return

        from modules.x402 import invoicing
        row = await invoicing.get_payment_request(inv["request_id"], db=self._db)
        if not row:
            return

        from modules.eip8004.payment_proof import proof_from_settled_invoice
        proof = proof_from_settled_invoice({
            "request_id": inv["request_id"],
            "tx_hash": tx_hash,
            "chain": row.get("chain"),
            "recipient": row.get("recipient"),
            "payer_address": cref.get("address"),
        })

        manager = self._reputation_mgr()
        auth = await manager.create_feedback_auth(
            client_address=cref.get("address"), task_id=inv["request_id"])

        from modules.x402.invoicing import _emit as _invoicing_emit
        _invoicing_emit(
            "payment_feedback_authorized", user_id=inv.get("user_id") or "",
            session_id=session_id, attrs={
                "request_id": inv["request_id"], "agent_id": auth.agentId})

        deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
        if deliver_corr is None:
            return
        src = f"{cref.get('surface', '')}:{cref.get('address', '')}"
        text = (
            "You can leave verified feedback for this payment on the "
            f"ERC-8004 Reputation Registry: agentId={auth.agentId}, "
            f"nonce={auth.nonce}, expiresAt={auth.expiresAt}, "
            f"signature={auth.signature}. Submit a score 0-100 to "
            "/eip8004/reputation/feedback with this authorization and proof "
            f"of payment (tx {proof.txHash}, chain {proof.chainId})."
        )
        delivered = await deliver_corr(
            session_id, src, text,
            {"kind_hint": "payment_feedback_authorization",
             "request_id": inv["request_id"]})
        if not delivered:
            logger.info(
                "settlement watcher: eip8004 feedback-authorization offer "
                "dropped for %s — the authorization was still created "
                "(not resent; not durably tracked beyond this log)",
                inv["request_id"])
