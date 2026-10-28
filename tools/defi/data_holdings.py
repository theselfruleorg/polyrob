"""``defi_data`` holdings reads — what a wallet holds and what it is worth.

The EVM and Solana holdings reports behind ``portfolio`` and
``wallet_holdings``, the target-address resolution for a third-party wallet,
and the gas lines. Split out of ``data_tool.py`` (W9, behaviour-preserving) as a
mixin, so every ``DefiDataTool._*`` name resolves as before. Module-level names
of ``data_tool`` are imported at call time, so a test that patches them there
(``_pinned_addresses``) still reaches this code.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from tools.defi.data_params import PortfolioParams, WalletHoldingsParams
from tools.defi.data_render import (
    _excluded_value_text, _fmt_amount, _fmt_usd, _holding_md, _holdings_metadata,
    _holdings_rest_lines, _ticker_collision_lines, _total_line, _valued_lines)
from tools.defi.providers import alchemy_index
from tools.defi.token_screen import quoted

logger = logging.getLogger("tools.defi.data_tool")


class HoldingsReadsMixin:
    """Mixed into :class:`tools.defi.data_tool.DefiDataTool`."""

    def _evm_holdings_report(self, holder: str, chain: str, *, own: bool = True):
        """The EVM holdings render for ANY holder — the agent's own wallet
        (`portfolio`, ``own=True``) or an explicit public address
        (`wallet_holdings`, ``own=False``). Only the wording about whose
        balance it is differs; coverage, budget and valuation are one code path."""
        from tools.defi.data_tool import (  # call-time
            SUPPORTED_CHAINS,
            _budget_spent,
            _pinned_addresses,
            _valuation_priority)
        if chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        # The budget clock starts BEFORE enumeration: the index/balance read is
        # part of the wall time the controller's 60 s guard measures.
        _budget_started = time.monotonic()
        indexed = None
        index_complete = True
        if self._index_fn is not None:
            indexed = self._index_fn(holder, chain=chain)
        elif alchemy_index.available():
            got = alchemy_index.fetch_all_balances(holder, chain=chain)
            if got is not None:
                indexed, index_complete = got

        if indexed is not None:
            raw = {a: v for a, v in indexed.items()}
            coverage = ("indexed (alchemy) — complete" if index_complete else
                        f"indexed (alchemy) — PARTIAL: {len(raw)} token(s) read, more "
                        f"pages exist or a page failed; a token not listed may still be held")
            scanned_note = ""
        else:
            scan = self._scan_set(chain)
            raw = self._balances(holder, chain, scan)
            coverage = f"partial — {len(scan)} candidate address(es) scanned"
            scanned_note = ("\n  (no ALCHEMY_API_KEY, so holdings could not be enumerated; "
                            "a token outside the scanned set is INVISIBLE here)")

        from core.wallet.tokens import canonical_token
        valued, unpriced_known, total = [], [], 0.0
        ours_unvalued, other_unvalued = [], []
        read_failed_other = zero_rows = 0
        # (symbol, address) for every row, so one symbol held under two
        # contracts can be flagged below — the 2026-09-23 defect, where a rail
        # resolved the token it was monitoring by ticker off this very screen.
        symbols_seen = []
        # 2026-09-23: the pricing half is one network call PER HOLDING, and this
        # wallet's dust cohort made that loop outlive the agent's 600 s stall
        # limit twice, so the SAFETY monitor wrote nothing. Bound it, and NAME
        # (or count) whatever the bound skipped — never shorten the list silently.
        # The clock started before enumeration (top of this method).
        budget_skipped: List[tuple] = []
        priced_count = 0
        pinned = _pinned_addresses(chain)
        # 071 §3.4: one batch call per source per 30 tokens BEFORE the loop
        # (inside the same budget clock), so a dust token with no pool costs no
        # per-row call and the second source costs no per-row call at all.
        prefetch = self._prefetch_prices(chain, [
            a for a, u in sorted(raw.items(), key=lambda kv: (
                _valuation_priority(chain, kv[0], pinned), kv[0])) if u is not None])
        md_rows: List[Dict[str, Any]] = []
        for addr, units in sorted(
                raw.items(),
                key=lambda kv: (_valuation_priority(chain, kv[0], pinned), kv[0])):
            # A token on the registry's CANONICAL pin list is one an operator
            # verified on-chain — it is definitionally not something a stranger
            # sent you. Its provenance and its PRICE are separate questions, and
            # a third-party indexer is allowed to fail at the second. Keeping
            # the two apart matters: on 2026-08-25 a DexScreener outage put the
            # wallet's own USDC under the dust warning, and the agent duly
            # reported its entire spendable balance as "not a position".
            known = canonical_token(chain, addr) is not None
            ours = _valuation_priority(chain, addr, pinned) == 0
            sink = unpriced_known if known else ours_unvalued if ours else other_unvalued
            if units is None:
                md_rows.append({"address": addr, "status": "balance_unknown"})
                if known or ours:
                    sink.append(f"  {addr}  balance UNKNOWN (read failed — not zero)")
                else:
                    # A scan-set candidate whose read failed is COUNTED, not
                    # listed: the scan set can carry placeholder addresses from
                    # a polluted cache, and a real holding is never hidden by
                    # this (only failed reads of unpinned candidates are).
                    read_failed_other += 1
                continue
            if units == 0:
                zero_rows += 1
                md_rows.append({"address": addr, "raw": "0", "status": "zero"})
                continue
            # ⚠️ BEFORE the identity lookup, not after it (2026-09-24). Identity
            # resolution is itself one network call PER HOLDING — measured at
            # 0.26 s mean, ~22 s across this wallet's 84 rows — so a check placed
            # after it leaves the larger half of the loop unbounded, and the
            # budget could not do its job however it was tuned.
            if _budget_spent(_budget_started, time.monotonic()):
                budget_skipped.append((addr, None, None, units))
                md_rows.append({"address": addr, "raw": str(units),
                                "status": "not_priced_budget"})
                continue
            ident = self._identity(chain, addr)
            # getattr: `_identity` is an injection seam (`self._identity_fn`),
            # and a caller's identity object is only promised symbol/decimals.
            # A missing name renders "name unknown"; demanding one would make
            # the collision check the thing that breaks the portfolio read.
            symbols_seen.append((ident.symbol, addr, getattr(ident, "name", None)))
            if ident.decimals is None:
                sink.append(f"  {addr}  {units} raw units (decimals unknown — cannot value; "
                            f"do not convert by hand)")
                md_rows.append({"address": addr, "symbol": ident.symbol, "raw": str(units),
                                "status": "decimals_unknown"})
                continue
            amount = units / (10 ** ident.decimals)
            priced_count += 1
            info = self._row_price(chain, addr, prefetch, ours=ours)
            if info.price_usd is None or info.confidence != "high":
                sink.append(
                    f"  {addr}  {_fmt_amount(amount)} {quoted(ident.symbol) or '?'}  "
                    f"{_excluded_value_text(amount, info, ours=ours)}")
                md_rows.append(_holding_md(addr, ident.symbol, ident.decimals, units,
                                           amount, info, None))
                continue
            value = amount * info.price_usd
            total += value
            valued.append((value, f"  {addr}  {_fmt_amount(amount)} {quoted(ident.symbol) or '?'}  "
                                  f"= {_fmt_usd(value)}"))
            md_rows.append(_holding_md(addr, ident.symbol, ident.decimals, units,
                                       amount, info, value))

        lines = [f"holdings for {holder} (chain {chain})",
                 f"coverage: {coverage}{scanned_note}", ""]
        native_out: Dict[str, Any] = {}
        lines += self._gas_lines(holder, chain, native_out=native_out) + [""]
        lines += _valued_lines(valued)
        # 079 W1: below the valued rows — the money is read first. Still above
        # the total, so it arrives before any decision made off this screen.
        lines += _ticker_collision_lines(symbols_seen)
        reads = len(raw)
        failed_reads = sum(1 for u in raw.values() if u is None)
        lines += [""] + _totals_with_native(
            _total_line(total, reads=reads, failed=failed_reads), total,
            all_failed=bool(reads and failed_reads >= reads), native=native_out)
        # Immediately after the total, because the total is the number a reader
        # acts on and it is INCOMPLETE whenever this section is non-empty.
        lines += _holdings_rest_lines(
            own=own, known=unpriced_known, ours=ours_unvalued, others=other_unvalued,
            budget_rows=budget_skipped, priced=priced_count, read_failed=read_failed_other)
        md = _holdings_metadata(
            holder=holder, chain=chain, own=own, coverage=coverage,
            complete=(indexed is not None and index_complete) and not budget_skipped,
            total_usd=total, rows=md_rows)
        md["native_usd"] = native_out.get("usd")
        return self._ar(content="\n".join(lines), metadata=md)

    def _own_addresses(self) -> List[str]:
        out: List[str] = []
        if self._holder != "__from_wallet__":
            return [str(self._holder)] if self._holder else []
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
        except Exception:
            wallet = None
        if wallet is None:
            return out
        for attr in ("address", "solana_address"):
            try:
                val = getattr(wallet, attr, None)
            except Exception:
                val = None
            if val:
                out.append(str(val))
        # 071 review: EVERY address the agent holds is an owner read — the venue
        # keys (x402, polymarket, hyperliquid) and the token-bound accounts of
        # the NFTs it owns, not only the operational and Solana addresses.
        try:
            from core.wallet.agent_wallet import VENUES
            for venue in sorted(VENUES):
                try:
                    val = wallet.address_for_venue(venue)
                except Exception:
                    val = None
                if val:
                    out.append(str(val))
        except Exception:
            logger.debug("venue addresses unreadable", exc_info=True)
        try:
            from core.wallet import nft_holdings
            for row in nft_holdings.tracked() or []:
                if row.get("account"):
                    out.append(str(row["account"]))
        except Exception:
            logger.debug("NFT account list unreadable", exc_info=True)
        return list(dict.fromkeys(out))

    def _address_on_chain(self, raw_address: str, raw_chain: Optional[str]):
        """``(address, chain, error)``: a base58 address defaults to solana, a
        0x address must name its chain, and the address is validated for it."""
        from core.wallet import chains
        raw = (raw_address or "").strip()
        chain = (raw_chain or "").strip().lower() or None
        if chain is None:
            if raw.lower().startswith("0x"):
                evm = [r.name for r in chains.all_rows() if r.family == "evm"]
                return None, None, (
                    f"{raw} is an EVM address, and the same address holds different "
                    f"things on each EVM chain. Call again with chain= one of: "
                    f"{', '.join(evm)} - for example "
                    f"wallet_holdings(address='{raw}', chain='{evm[0]}'). "
                    f"Repeating this call without chain fails the same way.")
            chain = "solana"
        address, err = self._validate(chain, raw)
        return address, chain, err

    def _wallet_target(self, raw_address: str, raw_chain: Optional[str], execution_context):
        """``(address, chain, own, error)`` for every any-wallet read
        (wallet_holdings, wallet_activity): chain inference, the own-address
        owner gate (CR-M07) and the address-kind check, in that order."""
        from tools.defi.data_tool import _operator_read_refusal  # call-time
        from core.wallet.address_kind import wrong_kind_for_wallet
        from core.wallet.addresses import same_address
        address, chain, err = self._address_on_chain(raw_address, raw_chain)
        if err:
            return None, None, False, err
        own = any(same_address(address, mine) for mine in self._own_addresses())
        if own:
            # CR-M07 unchanged: the agent's OWN wallet is an owner read, by
            # whichever verb and however the address was spelled.
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return None, None, True, refusal.split(" Another wallet")[0]
        wrong = wrong_kind_for_wallet(chain, address, self._address_kind(chain, address))
        if wrong:
            return None, None, own, wrong
        return address, chain, own, None

    def _wallet_holdings_sync(self, params: WalletHoldingsParams, execution_context=None):
        from core.wallet import chains
        address, chain, own, err = self._wallet_target(params.address, params.chain,
                                                       execution_context)
        if err:
            return self._ar(error=err)
        row = chains.get(chain)
        if row is not None and row.family == "svm":
            return self._solana_holdings_report(address, chain, own=own)
        return self._evm_holdings_report(address, chain, own=own)

    def _solana_portfolio_sync(self, params: PortfolioParams):
        """The agent's OWN holdings on a Solana-family chain (Phase 2)."""
        holder = self._solana_holder()
        if not holder:
            return self._ar(error=(
                "the agent wallet has no Solana address — set "
                "AGENT_WALLET_ENABLED=true with a BIP-39 master seed. Reads "
                "that need no address (token_info, price, new_pools, trending, "
                "wallet_holdings) work on solana regardless."))
        return self._solana_holdings_report(holder, params.chain, own=True)

    def _solana_holdings(self, holder: str):
        """`SplHoldings` (classic + Token-2022, decimals kept) or ``None``."""
        from core.wallet import solana_onchain
        try:
            return (self._solana_holdings_fn or solana_onchain.token_holdings)(holder)
        except Exception:
            return None

    def _solana_holdings_report(self, holder: str, chain: str, *, own: bool = True):
        """Holdings of ANY Solana owner. Deliberately its own method rather
        than a branch inside the EVM one: the enumeration is a different RPC,
        the gas asset is SOL, and it needs NO indexer key —
        `getTokenAccountsByOwner` returns every balance directly.

        071 R2/R3/R9: decimals come from the chain (an unpinned mint used to
        render as "raw units"), Token-2022 accounts are listed (the agent's own
        SPL launches are Token-2022), identity is read before price, and the
        pricing loop runs under the same wall-clock budget as the EVM one.
        """
        from tools.defi.data_tool import (  # call-time
            _budget_spent,
            _pinned_addresses,
            _valuation_priority)
        # The budget clock starts BEFORE enumeration: the token-account read is
        # part of the wall time the controller's 60 s guard measures.
        started = time.monotonic()
        native = self._solana_native(holder)
        held = self._solana_holdings(holder)
        lines = [f"holdings for {holder} (chain {chain})"]
        if held is None:
            lines += ["coverage: UNAVAILABLE — the token-account read failed. "
                      "This is NOT 'holds no tokens'; nothing below is a "
                      "complete picture.", ""]
        elif not held.token_2022_read:
            lines += ["coverage: PARTIAL — classic SPL accounts read; the "
                      "Token-2022 read FAILED, so Token-2022 holdings are "
                      "missing, not absent.", ""]
        else:
            lines += ["coverage: complete (SPL + Token-2022, enumerated from the "
                      "chain; no indexer key needed here)", ""]
        sol_native: Dict[str, Any] = {}
        if native is None:
            sol_native["note"] = "SOL balance UNKNOWN"
            lines.append("  SOL: balance UNKNOWN — the read failed. This "
                         "is NOT a zero.")
        elif native == 0:
            lines.append("  SOL: 0 — no SOL, the gas tank is EMPTY. Every Solana "
                         "transaction pays a fee in SOL, and a first-time token "
                         "transfer also funds rent, so nothing can be sent from here.")
        else:
            lines.append(self._native_money_line(chain, "SOL", native, ",.9f",
                                                 sol_native, label="SOL (gas + buying power)"))
        lines.append("")

        valued, total = [], 0.0
        ours_unvalued, other_unvalued = [], []
        budget_skipped: List[tuple] = []
        priced = 0
        sol_pinned = _pinned_addresses(chain)
        rows = sorted((held.rows if held else []),
                      key=lambda h: (_valuation_priority(chain, h.mint, sol_pinned), h.mint))
        # 071 §3.4: batch the price sources before the loop (see the EVM path).
        sol_prefetch = self._prefetch_prices(chain, [h.mint for h in rows])
        sol_md: List[Dict[str, Any]] = []
        for h in rows:
            if not h.raw:
                # An empty token account is not a holding: hidden, kept in metadata.
                sol_md.append({"address": h.mint, "raw": "0", "status": "zero"})
                continue
            # ⚠️ The budget is checked BEFORE the identity read (one network call
            # per row), and for EVERY row — also one whose amount is unknown.
            if _budget_spent(started, time.monotonic()):
                amt = h.raw / (10 ** h.decimals) if h.decimals is not None else None
                budget_skipped.append((h.mint, amt, None) if amt is not None
                                      else (h.mint, None, None, h.raw))
                sol_md.append({"address": h.mint, "decimals": h.decimals,
                               "raw": str(h.raw), "amount_human": amt,
                               "status": "not_priced_budget"})
                continue
            try:
                ident = self._identity(chain, h.mint)
            except Exception:
                ident = None
            decimals = getattr(ident, "decimals", None)
            if decimals is None:
                decimals = h.decimals
            symbol = getattr(ident, "symbol", None)
            amount = h.raw / (10 ** decimals) if decimals is not None else None
            label = f" {quoted(symbol)}" if symbol else ""
            t22 = (f"  [Token-2022{': ' + ', '.join(h.extensions) if h.extensions else ''}]"
                   if h.token_2022 else "")
            shown = (f"{_fmt_amount(amount)}{label}" if amount is not None
                     else f"{h.raw} raw units (decimals not read — do not convert by hand)")
            sol_ours = _valuation_priority(chain, h.mint, sol_pinned) == 0
            info = self._row_price(chain, h.mint, sol_prefetch, ours=sol_ours)
            priced += 1
            if info.price_usd is None or info.confidence != "high" or amount is None:
                (ours_unvalued if sol_ours else other_unvalued).append(
                    f"  {h.mint}  {shown}{t22}  "
                    f"{_excluded_value_text(amount, info, ours=own and sol_ours)}")
                sol_md.append(_holding_md(h.mint, symbol, decimals, h.raw, amount, info, None))
                continue
            value = amount * info.price_usd
            total += value
            valued.append((value, f"  {h.mint}  {shown}  = {_fmt_usd(value)}{t22}"))
            sol_md.append(_holding_md(h.mint, symbol, decimals, h.raw, amount, info, value))
        lines += _valued_lines(valued)
        lines += [""] + _totals_with_native(_total_line(total, reads=0, failed=0), total,
                                            all_failed=False, native=sol_native)
        lines += _holdings_rest_lines(
            own=own, known=[], ours=ours_unvalued, others=other_unvalued,
            budget_rows=budget_skipped, priced=priced)
        if any(h.token_2022 for h in rows if h.raw):
            lines += ["", "Token-2022 extensions (transfer fee, hook, permanent delegate, "
                          "non-transferable) can change what a balance is worth or whether it moves."]
        if own:
            lines += ["", "NOTE: this verb only reads. Selling goes through "
                          "defi_trade.solana_swap (guarded, dry_run by default), "
                          "and only when SOLANA_TRADE_ENABLED is armed."]
        sol_complete = bool(held is not None and held.token_2022_read and not budget_skipped)
        md = _holdings_metadata(
            holder=holder, chain=chain, own=own,
            coverage=("complete" if sol_complete else
                      "unavailable" if held is None else "partial"),
            complete=sol_complete, total_usd=total, rows=sol_md)
        md["native_sol"] = native
        md["native_usd"] = sol_native.get("usd")
        return self._ar(content="\n".join(lines), metadata=md)

    def _solana_holder(self) -> Optional[str]:
        if self._holder != "__from_wallet__":
            return None if self._holder is None else str(self._holder)
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            return None if wallet is None else wallet.solana_address
        except Exception:
            return None

    def _solana_native(self, holder: str):
        from core.wallet import solana_onchain
        try:
            return solana_onchain.native_balance(holder)
        except Exception:
            return None

    def _solana_tokens(self, holder: str):
        from core.wallet import solana_onchain
        try:
            return solana_onchain.token_balances(holder)
        except Exception:
            return None

    def _gas_lines(self, holder: str, chain: str, *,
                   native_out: Optional[Dict[str, Any]] = None) -> List[str]:
        """The native-coin row. NEVER omitted (proposal 029 R2).

        This block exists because of a live failure, not a feature request. The
        prod agent escalated "no ETH row in the portfolio — the gas tank may be
        empty" ten times over two weeks while the wallet held ~1,130 swaps worth
        of gas. `portfolio` enumerated ERC-20s only, so a MISSING row read as an
        EMPTY tank, and the agent (correctly, given what it could see) treated
        every exit as gas-blocked.

        So the row is unconditional, and the three states are kept apart in
        words: a number, "UNKNOWN" (read failed), and "EMPTY" (a real zero, the
        only one of the three that actually blocks a broadcast).

        079 W1: the native coin is MONEY. Only the chain's fee reserve
        (`chains.fee_reserve`) is kept back for gas; the rest is buying power, so
        the row carries its USD value and ``native_out`` hands that value to
        the ``total incl. native`` line. On 2026-10-12 the old wording ("Not a
        position and not counted in the total") made the agent tell the owner
        0.14 ETH was "not there".
        """
        from core.wallet import chains
        row = chains.get(chain)
        symbol = row.native_symbol if row else "native"
        out = native_out if native_out is not None else {}
        try:
            native = self._native_balance(holder, chain)
        except Exception as exc:                       # fail-open: lose the row, not the report
            logger.debug("portfolio: native balance read failed on %s (%s)", chain, exc)
            native = None
        if native is None:
            out["note"] = f"{symbol} balance UNKNOWN"
            return [f"  {symbol}: balance UNKNOWN — the read failed. "
                    f"This is NOT a zero — never read a failed read as an unfunded tank."]
        if native == 0:
            return [f"  {symbol}: 0 — the gas tank is EMPTY. No transaction can "
                    f"broadcast on {chain}, entries and EXITS alike, until it is funded."]
        label = f"{symbol} (gas + buying power)" if chain == "robinhood" else symbol
        return [self._native_money_line(chain, symbol, native, ",.6f", out, label=label)]

    def _native_price(self, chain: str) -> Optional[float]:
        """The native coin's USD price through its wrapped native — the proxy
        ``tx_guard`` values native by — or None. Only a ``high`` confidence quote
        counts, the same bar a token row must clear; never an invented figure."""
        from core.wallet import chains
        row = chains.get(chain)
        proxy = getattr(row, "wrapped_native", None) if row else None
        if not proxy or getattr(row, "valueless", False):
            return None
        try:
            info = self._price_for(chain, proxy)
        except Exception:
            logger.debug("native price unreadable on %s", chain, exc_info=True)
            return None
        price = getattr(info, "price_usd", None)
        if price is None or getattr(info, "confidence", None) != "high":
            return None
        return float(price)

    def _native_money_line(self, chain: str, symbol: str, native: float, fmt: str,
                           out: Dict[str, Any], *, label: str) -> str:
        """``ETH 0.140662 ($352.10) — 0.138662 spendable above a 0.002 ETH fee
        reserve``. Writes ``out["usd"]`` when priced, else ``out["note"]``."""
        from core.wallet import chains
        price = self._native_price(chain)
        if price is None:
            out["note"] = f"{symbol} unpriced"
            worth = "(unpriced — not counted in the total)"
        else:
            out["usd"] = native * price
            worth = f"({_fmt_usd(native * price)})"
        reserve = chains.fee_reserve(chain)
        if reserve is None:
            tail = (f"no fee reserve is set for {chain}; keep enough {symbol} "
                    f"for the fees of an exit")
        elif native > float(reserve):
            tail = (f"{format(native - float(reserve), fmt)} spendable above a "
                    f"{format(reserve, 'f')} {symbol} fee reserve")
        else:
            tail = (f"BELOW the {format(reserve, 'f')} {symbol} fee reserve: nothing "
                    f"is spendable and the gas tank is nearly EMPTY — fund it before "
                    f"an entry or an EXIT on {chain}")
        return f"  {label} {format(native, fmt)} {worth} — {tail}."


def _totals_with_native(token_line: str, token_total: float, *, all_failed: bool,
                        native: Dict[str, Any]) -> List[str]:
    """079 W1: ``total incl. native`` first, the token total as a sub-line.
    An unpriced or unread native coin is NAMED as not counted, never summed."""
    sub = "  tokens" + token_line[len("total"):]
    if all_failed:
        return ["total incl. native: UNKNOWN — the token balance reads failed", sub]
    usd = native.get("usd")
    if usd is not None:
        head = (f"total incl. native: {_fmt_usd(token_total + usd)} "
                f"(native {_fmt_usd(usd)} + tokens {_fmt_usd(token_total)})")
    elif native.get("note"):
        head = (f"total incl. native: {_fmt_usd(token_total)} — {native['note']}, "
                f"not counted")
    else:
        head = f"total incl. native: {_fmt_usd(token_total)}"
    return [head, sub]
