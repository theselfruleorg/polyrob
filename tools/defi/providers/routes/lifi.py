"""LI.FI as a ``RouteProvider`` — the reach extension (proposal 029).

Chosen over 1inch and 0x for one disqualifying reason: **both now require an
API key and prod holds none** (probed 2026-08-24 — `api.1inch.dev` and
`api.0x.org` each return `401`; OpenOcean returns a Cloudflare `403`). That is
the same wall that made proposal 023 abandon 0x for direct pool reads. LI.FI
answers keylessly on 69 EVM chains and, in the live probe, routed all three
tokens the V3-only rail had refused (via bitget / kyberswap / fly).

**What this module does NOT do.** It does not bridge. LI.FI's whole product is
cross-chain, and `fromChain == toChain` is enforced here on every request:
a bridge adds a settlement-delay failure mode `tx_guard` has no model for
(the value leaves on one chain and arrives later on another, so there is no
single transaction whose deltas can be asserted).

Trust is bounded, not extended: the returned calldata is opaque to us, the
spender is checked against the chain's pinned `aggregator_spender` by
`routes.best_route`, our own slippage floor overrides theirs, and the guard
still simulates and asserts the observed deltas. What LI.FI gets to decide is
the PATH; what it cannot decide is the outcome we accept.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from tools.defi.providers.routes import RouteQuote

logger = logging.getLogger(__name__)

QUOTE_URL = "https://li.quest/v1/quote"

#: How LI.FI names the native gas asset on the wire. Measured 2026-09-13 on
#: Robinhood (4663): this and 0xEeee…EEeE return an identical quote, so the zero
#: form is used as the unambiguous one. Confined to this module — our own rail
#: passes the `routes.NATIVE` word, never an address, so no magic 0x value ever
#: reaches the checksum/pin machinery.
_LIFI_NATIVE = "0x0000000000000000000000000000000000000000"

#: Deliberately short. An aggregator quote is more perishable than a pool read,
#: and a slow route is a stale price, so waiting longer buys a worse trade.
TIMEOUT_SEC = 12.0


def _int(value, default=None):
    """Provider numbers arrive as decimal STRINGS. A malformed one is None
    (unknown), never 0 (``tools.defi.providers._http.parse_int``)."""
    from tools.defi.providers._http import parse_int
    return parse_int(value, default)


class LifiRouteProvider:
    name = "lifi"

    def __init__(self, *, fetch=None):
        # Injected in tests so a unit run never touches the network.
        self._fetch = fetch or self._http_get

    def supports(self, chain: str) -> bool:
        from core.wallet import chains
        row = chains.get(chain)
        if row is None or "lifi" not in row.route_hints:
            return False
        # No pinned spender means the route would be refused downstream anyway;
        # saying so here saves a pointless network call.
        return bool(row.aggregator_spender)

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"accept": "application/json",
                          "user-agent": "polyrob-defi/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return json.loads(resp.read())

    def quote(self, chain: str, token_in: str, token_out: str,
              amount_in_raw: int, *, holder: str,
              slippage_bps: int) -> Optional[RouteQuote]:
        from core.wallet import chains
        row = chains.get(chain)
        if row is None:
            return None

        # ⚠️ LOWERCASE, deliberately. LI.FI's token index is case-sensitive on
        # some chains: measured on Robinhood (4663), the same pair quotes fine
        # lowercase and returns HTTP 404 "no available quotes" for the EIP-55
        # checksummed form. We checksum every EVM address on the way in — it is
        # the only typo detector Ethereum has — so we were handing the API the
        # one form it rejects, and a 404 fails open to "no route", which reads
        # as "this token is unbuyable" for a token that routes fine.
        #
        # Safe because this is OUTBOUND only and these are EVM hex addresses:
        # lowercase is their canonical unchecksummed form, we still hold the
        # checksummed value, and the spender/target that comes BACK is still
        # validated against the chain's pin. Never do this for base58 (Solana),
        # where case is data rather than presentation.
        # NATIVE-in: translate our sentinel to LI.FI's zero address at the edge.
        # Measured live on Robinhood 2026-09-13: LI.FI accepts BOTH 0x000…0 and
        # 0xEee…EEeE for native and returns an identical quote, so the zero form
        # is chosen as the one that is unambiguous on every chain. `token_out`
        # is never a sentinel — this rail buys a specific contract.
        from tools.defi.providers.routes import is_native
        from_token = (_LIFI_NATIVE if is_native(token_in) else token_in.lower())

        params = {
            "fromChain": str(row.chain_id),
            # Same-chain ONLY. See the module docstring: bridging is out of scope.
            "toChain": str(row.chain_id),
            "fromToken": from_token,
            "toToken": token_out.lower(),
            "fromAmount": str(int(amount_in_raw)),
            "fromAddress": holder.lower(),
            "slippage": str(slippage_bps / 10_000.0),
        }
        url = f"{QUOTE_URL}?{urllib.parse.urlencode(params)}"
        try:
            body = self._fetch(url)
        except Exception as exc:
            # Fail open to None: "no route" is unknown. The caller must never
            # read an outage as "this pair is untradeable".
            logger.info("lifi: no quote for %s->%s on %s (%s)",
                        token_in, token_out, chain, exc)
            return None
        if not isinstance(body, dict):
            return None

        estimate = body.get("estimate") or {}
        tx = body.get("transactionRequest") or {}
        amount_out = _int(estimate.get("toAmount"))
        if not amount_out or amount_out <= 0:
            return None
        calldata = tx.get("data") or ""
        to = tx.get("to") or ""
        spender = estimate.get("approvalAddress") or ""
        if not calldata or not to or not spender:
            logger.warning("lifi: quote missing to/data/approvalAddress — refused")
            return None

        # The chain the provider says it built for must be the chain we asked
        # about. A mismatch means a bridge route or a repointed API, and the
        # signer would sign for the wrong chain id.
        tx_chain = _int(tx.get("chainId"))
        if tx_chain is not None and tx_chain != row.chain_id:
            logger.error("lifi: quote is for chain %s, not %s — REFUSED",
                         tx_chain, row.chain_id)
            return None

        value_raw = _int(tx.get("value"), 0)
        if isinstance(tx.get("value"), str) and tx["value"].startswith("0x"):
            value_raw = int(tx["value"], 16)
        # An ERC-20 -> ERC-20 swap moves no native value. A non-zero value here
        # would be an unasserted native outflow riding along with the trade.
        #
        # ⚠️ "Unasserted" was the whole objection, and for a NATIVE-in swap it no
        # longer holds: `tx_guard` learned to declare and assert a native send in
        # 039 B1 (`TxIntent.token=None` measures the native delta and refuses a
        # short or long move against the same pinned dust tolerance). So a native
        # quote carries value by definition and is passed through with the value
        # INTACT — `best_route` then holds it to exactly `amount_in_raw`. Keeping
        # the blanket refusal here is what forced every entry through WETH, and
        # therefore through an allowance the registry says is unnecessary.
        if value_raw and not is_native(token_in):
            logger.error("lifi: quote carries native value %s on an ERC-20 swap "
                         "— REFUSED", value_raw)
            return None

        tool = body.get("tool") or "unknown"
        return RouteQuote(
            chain=chain, token_in=token_in, token_out=token_out,
            amount_in_raw=amount_in_raw, amount_out_raw=amount_out,
            amount_out_min_raw=_int(estimate.get("toAmountMin")),
            spender=spender, to=to, calldata=calldata,
            # Zero for an ERC-20 swap (asserted above); the native amount for a
            # native-in swap, which `best_route` holds to `amount_in_raw`.
            value_raw=(value_raw if is_native(token_in) else 0),
            venue=f"lifi:{tool}", quoted_at=time.time(),
            locally_built=False)
