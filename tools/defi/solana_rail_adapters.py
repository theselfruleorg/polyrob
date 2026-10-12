"""The Solana rail adapters behind ``defi_trade``'s Solana verbs.

Each method is ONE seam to the chain or a provider (Jupiter quote and build,
the vetted simulation, the GoPlus screen, the blockhash check, the signed send,
the confirmation, balances, decimals and the USD valuation ladder), and each is
injectable through the ``solana_*_fn`` arguments of ``DefiTradeTool.__init__``.

Split out of ``trade_tool.py`` (W9, behaviour-preserving) as a mixin, so
``DefiTradeTool._solana_simulate`` and the other names resolve as before and a
test that patches them on the class still reaches every caller.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("tools.defi.trade_tool")


class SolanaRailMixin:
    """Mixed into :class:`tools.defi.trade_tool.DefiTradeTool`; reads the
    ``_solana_*_fn`` overrides, ``_price`` and ``_fallback_price`` from it."""

    def _solana_usdc_mint(self):
        from core.wallet import chains
        row = chains.get("solana")
        return row.usdc if row else None

    def _solana_held_raw(self, owner: str, mint: str):
        """Held balance of *mint* in raw units. None = UNKNOWN (never zero).

        wSOL folds in the native SOL balance — Jupiter wraps through it, so the
        balance behind a SOL sell is native SOL plus any wrapped account.
        """
        if self._solana_held_fn:
            try:
                return self._solana_held_fn(owner, mint)
            except Exception:
                return None
        from core.wallet import solana_onchain
        try:
            balances = solana_onchain.token_balances(owner)
        except Exception:
            balances = None
        from tools.defi.trade_tool import _WSOL_MINT  # call-time
        if mint == _WSOL_MINT:
            try:
                sol = solana_onchain.native_balance(owner)
            except Exception:
                sol = None
            if sol is None:
                return None
            return int(sol * 1_000_000_000) + int((balances or {}).get(mint, 0))
        if balances is None:
            return None
        return int(balances.get(mint, 0))

    def _solana_value_usd(self, *, token_in, amount_in, token_out,
                          out_delta_raw, usdc_mint, exit_bounded_fn, valuation=None):
        """USD value of the outflow, EVM-parity ladder (tx_guard step 7):
        pinned USDC = $1.00 by definition → high-confidence price →
        (exit-bounded) fallback price → (exit-bounded sell-to-USDC) the
        simulation's measured quote inflow. None = unpriceable → refuse.
        """
        if usdc_mint and token_in == usdc_mint:
            return float(amount_in)
        try:
            px = self._price("solana", token_in)
        except Exception:
            px = None
        exit_bounded = False
        if px is None:
            try:
                exit_bounded = bool(exit_bounded_fn())
            except Exception:
                exit_bounded = False
            if exit_bounded:
                try:
                    px = self._fallback_price("solana", token_in)
                except Exception:
                    px = None
        if px is not None:
            return float(amount_in) * float(px)
        if (exit_bounded and usdc_mint and token_out == usdc_mint
                and out_delta_raw and out_delta_raw > 0):
            out_dec = self._identity_solana(token_out)
            if out_dec is not None:
                value = out_delta_raw / (10 ** out_dec)
                if valuation is not None:
                    valuation["valuation_basis"] = "inflow"
                logger.info(
                    "defi.solana_swap exit_inflow_valuation token_in=%s "
                    "amount_usd=%.2f — outflow unpriceable by any source; caps "
                    "run against the measured USDC receipt", token_in, value)
                return value
        return None

    def _identity_solana(self, mint: str) -> Optional[int]:
        """Decimals for an SPL mint, READ from the chain, or None to refuse.

        This used to default to 6, which was a guess on a SIZING path and wrong
        for a large share of mints — wSOL is 9, so a 0.003 SOL swap sized at 6
        decimals becomes 0.000003 SOL, a 1000x error. The EVM path refuses in
        exactly this situation ("refusing to size a swap against a token whose
        denomination is unknown") and this was the one place the Solana path
        guessed instead. `getTokenSupply` returns the mint's decimals directly,
        so there was never a reason to.
        """
        if self._solana_decimals_fn:
            return self._solana_decimals_fn(mint)
        try:
            from core.wallet import solana_onchain
            res = solana_onchain._rpc("getTokenSupply", [mint])
            decimals = ((res or {}).get("value") or {}).get("decimals")
            return int(decimals) if decimals is not None else None
        except Exception:
            return None

    def _solana_quote(self, token_in, token_out, amount_in_raw, slippage_bps):
        if self._solana_quote_fn:
            return self._solana_quote_fn(token_in, token_out, amount_in_raw,
                                         slippage_bps=slippage_bps)
        from tools.defi.providers import jupiter
        return jupiter.quote(token_in, token_out, amount_in_raw,
                             slippage_bps=slippage_bps)

    def _solana_build(self, quote, holder):
        if self._solana_build_fn:
            return self._solana_build_fn(quote, holder)
        from tools.defi.providers import jupiter
        return jupiter.build_swap(quote.raw, holder)

    def _solana_simulate(self, *, raw_tx, owner, mints=(), extra_allowed=frozenset(),
                         locally_built_transfer=False, swap_bounds=None):
        """Vet the bytes, then simulate them against everything we own.

        ⚠️ The addresses MUST be the token accounts, not the owner. SPL
        balances live in accounts the owner merely owns, so asking for the
        owner's system account returns no token balances and `parse_deltas`
        then sees an EMPTY delta set — which reads as "nothing moved" and
        passes. Observing nothing is not the same as verifying nothing moved,
        and that distinction is the whole point of simulating.

        The address set, the Token-2022-aware ATA derivation and the top-level
        program allowlist all live in `core.wallet.solana_tx_inspect` — the
        earlier inline version derived the ATA under the classic SPL Token
        program alone (so a Token-2022 mint was never observed) and named only
        the accounts the CALLER declared (so an instruction touching a
        different wallet-owned account was invisible).
        """
        if self._solana_simulate_fn:
            return self._solana_simulate_fn(raw_tx=raw_tx, owner=owner)
        from core.wallet import solana_tx_inspect
        from core.wallet.solana_rail import SolanaRail
        rail = SolanaRail(signer=None)
        return solana_tx_inspect.simulate(
            raw_tx, owner=owner, mints=mints, rpc=rail._rpc,
            extra_allowed=extra_allowed, locally_built_transfer=locally_built_transfer,
            swap_bounds=swap_bounds)

    def _solana_confirm(self, signature):
        """``(ok, detail)`` for a broadcast signature — the EVM `await_receipt`
        mirror. Bounded (the rail polls a fixed number of attempts) and never
        resends: an UNKNOWN status usually means an expired blockhash, and a
        blind retry is how a double-send happens.
        """
        if self._solana_confirm_fn:
            return self._solana_confirm_fn(signature)
        from core.wallet.solana_rail import SolanaRail
        return SolanaRail(signer=None).confirm(signature)

    def _solana_screen(self, mint):
        """The GoPlus Solana screen for *mint* (CR-L10); injectable."""
        if self._solana_screen_fn:
            return self._solana_screen_fn(mint)
        from tools.defi.providers import goplus
        return goplus.screen("solana", mint)

    def _solana_blockhash_valid(self, raw_tx):
        """``(valid, detail)`` for the blockhash baked into *raw_tx* (CR-M05)."""
        if self._solana_blockhash_fn:
            return self._solana_blockhash_fn(raw_tx)
        from solders.transaction import VersionedTransaction
        from core.wallet.solana_rail import SolanaRail
        blockhash = str(VersionedTransaction.from_bytes(bytes(raw_tx))
                        .message.recent_blockhash)
        rail = SolanaRail(signer=None)
        res = rail._rpc("isBlockhashValid",
                        [blockhash, {"commitment": rail.COMMITMENT}])
        valid = (res or {}).get("value") if isinstance(res, dict) else None
        if valid is True:
            return True, blockhash
        return False, f"blockhash {blockhash}: isBlockhashValid={valid!r}"

    def _solana_send(self, raw_tx, signer):
        if self._solana_send_fn:
            return self._solana_send_fn(raw_tx)
        from solders.transaction import VersionedTransaction
        from core.wallet.solana_rail import SolanaRail
        tx = VersionedTransaction.from_bytes(bytes(raw_tx))
        signed = signer.sign_transaction(tx)     # refuses a foreign fee payer
        return SolanaRail(signer=signer).send_raw(bytes(signed))
