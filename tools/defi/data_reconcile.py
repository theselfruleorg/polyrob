"""``defi_data.reconcile`` — the rail book compared with the chain, server-side.

The comparison itself, the rail book it reads, the render, and the
precondition-failure record. Split out of ``data_tool.py`` (W9,
behaviour-preserving) as a mixin, so every ``DefiDataTool._*`` name resolves as
before. Module-level names of ``data_tool`` are imported at call time, so a
test that patches them there (``_pinned_addresses``) still reaches this code.
"""
from __future__ import annotations

import time

from tools.defi.data_params import ReconcileParams
from tools.defi.providers import alchemy_index


class ReconcileReadsMixin:
    """Mixed into :class:`tools.defi.data_tool.DefiDataTool`."""

    def _record_precondition_failed(self, params, execution_context, reason: str) -> None:
        try:
            from core.event_kinds import RAIL_PRECONDITION_FAILED
            from core.event_log import emit
            emit(RAIL_PRECONDITION_FAILED, source="defi_data",
                 user_id=str(getattr(execution_context, "user_id", "") or ""),
                 session_id=str(getattr(execution_context, "session_id", "") or ""),
                 attrs={"tool": "reconcile", "chain": getattr(params, "chain", ""),
                        "reason": reason[:400]})
        except Exception:
            pass

    def _reconcile_impl_sync(self, params: ReconcileParams, execution_context=None):
        # The §0 fix (2026-08-25): runs answered from a stale or partial read
        # and nothing ever compared the book to the chain. This verb is that
        # comparison, server-side, so no step budget, context compaction, or
        # partial read can quietly turn "three positions" into "book flat".
        from tools.defi.data_tool import SUPPORTED_CHAINS, _operator_read_refusal  # call-time

        chain = params.chain
        if chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        from core.wallet import chains
        row = chains.get(chain)
        svm = row is not None and row.family == "svm"
        # CR-M07: the book is reconciled against the OPERATOR wallet — an owner
        # read.
        caller_refusal = _operator_read_refusal(execution_context)
        if self._holder == "__from_wallet__" and caller_refusal:
            return self._ar(error=caller_refusal)
        holder = self._solana_holder() if svm else self._resolve_holder()
        nft_account = (getattr(params, "account", None) or "").strip()
        if nft_account:
            # 050 §7.4: reconcile scans the account it is asked about.
            import re as _re
            if svm or not _re.fullmatch(r"0x[0-9a-fA-F]{40}", nft_account):
                return self._ar(error=(
                    f"account {nft_account!r} is not an EVM account address — a token-bound "
                    f"account is a 0x-prefixed 20-byte address on an EVM chain"))
            holder = nft_account
        if not holder:
            from core.wallet.config import WALLET_OFF_TEXT
            return self._ar(error=(
                WALLET_OFF_TEXT + " — "
                "no address to reconcile against"))

        rail, rail_state = self._rail_book(execution_context, chain,
                                           account=nft_account)
        if rail is None:
            # Unknown is not an empty book: nothing to compare against.
            return self._ar(error=f"the book was not read: {rail_state}")
        addr_rows = list(rail)

        # Chain side: the SAME seams `portfolio` uses. Under partial scan
        # coverage every EVM book address is added to the scan set, so a
        # book row always gets a direct read — absence can then only mean
        # "the complete index did not see it", i.e. a genuine zero.
        if svm:
            # getTokenAccountsByOwner enumerates every SPL balance directly, so
            # coverage is COMPLETE here with no indexer key. A None is a FAILED
            # read, never an empty wallet: reporting it as zero would turn "I
            # could not look" into "the position is gone", which is the precise
            # collapse reconcile exists to catch.
            enumerated = self._solana_tokens(holder)
            if enumerated is None:
                raw = {r.address: None for r in addr_rows}
                coverage = ("UNAVAILABLE — the token-account read failed. Every "
                            "row below is unverified; this is NOT a zero book.")
            else:
                raw = dict(enumerated)
                for r in addr_rows:
                    raw.setdefault(r.address, 0)
                coverage = ("complete (enumerated from the chain; no indexer "
                            "key needed here)")
            return self._render_reconcile(raw, chain=chain, row=row, holder=holder,
                                          coverage=coverage, rail=rail,
                                          rail_state=rail_state)
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
            coverage = "indexed (alchemy) — complete"
            if not index_complete:
                # 071 R4: a partial index must not make a held book row read
                # as "missing on chain" — read every book row directly.
                book_addrs = [r.address for r in addr_rows
                              if r.address.startswith("0x") and r.address not in raw]
                if book_addrs:
                    for a, v in self._balances(holder, chain, book_addrs).items():
                        raw.setdefault(a, v)
                coverage = ("indexed (alchemy) — PARTIAL (page cap or a failed page); "
                            "every book row was read directly, but an unlisted "
                            "token may still be held")
        else:
            scan = list(dict.fromkeys(
                self._scan_set(chain)
                + [r.address for r in addr_rows if r.address.startswith("0x")]))
            raw = self._balances(holder, chain, scan)
            coverage = (f"partial — {len(scan)} address(es) scanned (no "
                        f"ALCHEMY_API_KEY; a token outside this set is "
                        f"invisible here, but every book row was read "
                        f"directly)")

        return self._render_reconcile(raw, chain=chain, row=row, holder=holder,
                                      coverage=coverage, rail=rail,
                                      rail_state=rail_state)

    @staticmethod
    def _rail_uid(execution_context) -> str:
        uid = str(getattr(execution_context, "user_id", "") or "").strip()
        if not uid:
            try:
                from core.exec_identity import current_exec_identity
                uid = str(current_exec_identity()[0] or "").strip()
            except Exception:
                uid = ""
        if not uid:
            # The operator wallet's book is the owner's (the same fallback as
            # ``positions``); a non-owner never reaches here (CR-M07).
            try:
                from core.instance import resolve_owner_principal
                uid = str(resolve_owner_principal() or "").strip()
            except Exception:
                uid = ""
        return uid

    def _rail_book(self, execution_context, chain: str, *, account: str = ""):
        """071 W3: the rail store's rows on ``chain`` for this tenant and holder,
        as ``(rows, state)``. ``rows is None`` = not consulted — the state says
        why (no tenant, or an UNREADABLE store: unknown is not an empty book)."""
        from tools.defi import reconcile as rec
        uid = self._rail_uid(execution_context)
        if not uid:
            return None, "not consulted (no tenant on this call)"
        try:
            from core import open_positions
            entries = open_positions.positions_for(
                uid, chain=chain, account=(account or "").lower())
        except Exception as exc:
            return None, (f"UNREADABLE ({type(exc).__name__}) — not compared; "
                          f"this is NOT an empty rail book")
        rows = [rec.RailPosition(address=e.address, symbol=e.symbol, qty=e.qty,
                                 status=e.status) for e in entries]
        return rows, f"{len(rows)} tracked row(s) on {chain} compared"

    def _render_reconcile(self, raw, *, chain, row, holder, coverage, rail=None,
                          rail_state=None):
        """Value the chain side and diff it against the book. Family-agnostic
        on purpose: only the BALANCE SOURCE differs between EVM and Solana, and
        duplicating the valuation would let the two drift."""
        from tools.defi.data_tool import (  # call-time
            _DUST_MEMO,
            _DUST_MEMO_TTL_S,
            _RECONCILE_WORKERS,
            _pinned_addresses,
            _valuation_priority)
        from tools.defi import reconcile as rec
        holdings = []
        # 2026-10-03: priced one token at a time, an 80-token treasury (mostly
        # airdropped dust, each GeckoTerminal call a 429 + 2 s retry) passed the
        # 60 s action budget on every call and the gate STOPPED every buyback.
        # The holdings loop's batch prefetch prices the dust; a book or pinned
        # token is still asked of its primary source.
        pinned = _pinned_addresses(chain)
        booked = {str(getattr(r, "address", "")).lower() for r in rail or []}

        def _ours(addr):
            return addr.lower() in booked or _valuation_priority(chain, addr, pinned) == 0

        # Booked/pinned first, so the prefetch cap (PREFETCH_MAX) always covers them.
        held = sorted((a for a, u in (raw or {}).items() if u),
                      key=lambda a: (not _ours(a), a))
        prefetch = self._prefetch_prices(chain, held) if held else None

        memo_now = time.monotonic()

        def _value(item):
            addr, units = item
            key = (chain, addr.lower())
            if units and not _ours(addr):
                hit = _DUST_MEMO.get(key)
                if hit and hit[0] == units and memo_now - hit[2] < _DUST_MEMO_TTL_S:
                    return hit[1]
            holding = _value_fresh(addr, units)
            if units and not _ours(addr) and _is_noise(holding):
                _DUST_MEMO[key] = (units, holding, memo_now)
            else:
                _DUST_MEMO.pop(key, None)
            return holding

        def _is_noise(h):
            dust = rec.DUST_VALUE_USD
            if h.value_usd is not None:
                return h.value_usd < dust
            if h.price_state == "failed" or h.qty is None:
                return False
            if h.est_value_usd is not None:
                return h.est_value_usd < dust
            return h.price_state == "no_pool"

        def _value_fresh(addr, units):
            ident = self._identity(chain, addr)
            qty = value = est = None
            state = None
            if units is not None and getattr(ident, "decimals", None) is not None:
                qty = units / (10 ** ident.decimals)
            if units:
                info = self._row_price(chain, addr, prefetch, ours=_ours(addr))
                usd = getattr(info, "price_usd", None)
                if usd is not None:
                    state = "high" if getattr(info, "confidence", None) == "high" else "priced"
                    if qty is not None:
                        est = qty * usd
                        if state == "high":
                            value = est
                else:
                    state = "failed" if getattr(info, "failed", None) else "no_pool"
            return rec.ChainHolding(
                address=addr, symbol=getattr(ident, "symbol", None), qty=qty,
                raw_units=units, value_usd=value,
                balance_known=units is not None,
                name=getattr(ident, "name", None),
                price_state=state, est_value_usd=est)

        # Each row's identity reads and price quote are independent network
        # reads; serially, 116 rows took 93 s on prod (2026-10-03). `map` keeps
        # address order, so the report is identical to the serial one.
        from concurrent.futures import ThreadPoolExecutor
        items = sorted((raw or {}).items())
        with ThreadPoolExecutor(max_workers=_RECONCILE_WORKERS) as pool:
            holdings.extend(pool.map(_value, items))

        quote_addresses = [a for a in (
            row.usdc if row else None,
            row.wrapped_native if row else None) if a]
        # `evm_chain` decides which address FAMILY counts as this chain's rows;
        # leaving it True on Solana filed every base58 row as "other chain
        # family (not checked here)" and rendered a CLEAN verdict over an
        # unreconciled book — a false all-clear, the worst possible output here.
        report = rec.diff(rail or [], holdings, quote_addresses=quote_addresses,
                          evm_chain=(row is None or row.family == "evm"), chain=chain)
        return self._ar(content=rec.render(
            report, chain=chain, holder=holder, coverage=coverage,
            rail_state=rail_state),
            metadata={"report": report.to_dict(), "coverage": coverage,
                      "rail_state": rail_state})
