"""Relay.link as the ONE cross-chain provider (proposal 037).

Why Relay and not LI.FI. `routes/lifi.py` refuses cross-chain on purpose and
says why: a bridge has no single transaction whose deltas can be asserted. That
refusal stands — this module does not relax it, it answers it with a different
shape (a two-phase guard, `core/wallet/bridge_guard.py`). Relay was chosen over
routing through LI.FI because, probed live on 2026-09-11, LI.FI's own answer for
Solana -> Robinhood Chain is `"tool": "relaydepository"` — i.e. Relay. Going
through LI.FI would add a layer whose route choice we cannot pin, to reach the
settlement network we can talk to directly.

What Relay uniquely gives us:

* all three chains the owner asked for in ONE hop — Solana (792703809), Base
  (8453) and Robinhood Chain (4663). The four-step plan (swap, bridge, swap,
  bridge) collapses to one order, measured cheaper than the two-leg path.
* a structured `protocol.v2.orderData` — an order object, not opaque calldata.
* `/intents/status?requestId=` — the arrival oracle phase 2 of the guard needs.
* ~1-2s settlement estimates, which is what makes a synchronous arrival poll
  honest rather than a background job that outlives the turn.

TRUST IS BOUNDED, exactly as in `lifi.py`: Relay decides the PATH. It does not
decide the outcome we accept. Everything it returns is asserted here against
what we asked for — chain ids, recipient, sender, currencies — and refused on
any mismatch. A field we cannot read is UNKNOWN and refuses; it is never
defaulted to a permissive number.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

QUOTE_URL = "https://api.relay.link/quote"
STATUS_URL = "https://api.relay.link/intents/status"

#: A bridge quote is perishable like any other route quote, but a bridge also
#: crosses a chain boundary, so the request is heavier than a same-chain read.
TIMEOUT_SEC = 20.0

#: Relay's own id for Solana mainnet (not a real EVM chain id).
SOLANA_CHAIN_ID = 792703809

#: The native-asset sentinel per VM family.
NATIVE_EVM = "0x0000000000000000000000000000000000000000"
NATIVE_SVM = "11111111111111111111111111111111"

#: Terminal + non-terminal status vocabulary, normalised. Anything we do not
#: recognise is `unknown` — never optimistically mapped onto success.
_SUCCESS = frozenset({"success", "filled", "complete", "completed"})
_FAILURE = frozenset({"failure", "failed", "refund", "refunded", "expired"})
_PENDING = frozenset({"pending", "waiting", "delayed", "processing", "submitted"})


class RelayError(RuntimeError):
    """A quote/status call that did not produce a usable answer."""


@dataclass(frozen=True)
class BridgeQuote:
    """One Relay order, already asserted against what the caller asked for."""

    request_id: str
    origin_chain_id: int
    dest_chain_id: int
    sender: str
    recipient: str
    currency_in: str
    currency_out: str
    amount_in_raw: int
    amount_out_raw: int
    #: `amount_out_raw` minus Relay's own destination slippage allowance. This is
    #: the floor phase 2 asserts the ARRIVAL against.
    min_out_raw: int
    decimals_in: int
    decimals_out: int
    symbol_in: str
    symbol_out: str
    amount_in_usd: Optional[float]
    amount_out_usd: Optional[float]
    impact_pct: Optional[float]
    time_estimate_sec: Optional[int]
    #: Where the funds go on the ORIGIN chain. Asserted by phase 1.
    deposit_address: Optional[str]
    #: The signable payload. EVM: {to, data, value, chainId}. SVM: {instructions, …}.
    tx_data: Dict[str, Any]
    #: True when the ORIGIN is Solana, i.e. the SVM signer path.
    svm_origin: bool
    raw: Dict[str, Any]

    @property
    def amount_out_formatted(self) -> float:
        return self.amount_out_raw / (10 ** self.decimals_out)

    @property
    def min_out_formatted(self) -> float:
        return self.min_out_raw / (10 ** self.decimals_out)

    @property
    def amount_in_formatted(self) -> float:
        return self.amount_in_raw / (10 ** self.decimals_in)


def _post(url: str, body: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json",
                 "user-agent": "polyrob-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, *, timeout: float) -> Dict[str, Any]:
    from tools.defi.providers._http import get_json
    return get_json(url, timeout=timeout, user_agent="polyrob-bridge/1.0")


def _int(value, default=None):
    """Relay numbers arrive as decimal STRINGS. A malformed one is None
    (unknown), never 0 (``tools.defi.providers._http.parse_int``)."""
    from tools.defi.providers._http import parse_int
    return parse_int(value, default)


def _float(value, default=None):
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


class RelayBridgeProvider:
    """Quote and status over `api.relay.link`. Injectable HTTP for tests."""

    name = "relay"

    def __init__(self, *, post=None, get=None, timeout: float = TIMEOUT_SEC):
        self._post = post or _post
        self._get = get or _get
        self._timeout = timeout

    # -- quote ------------------------------------------------------------

    def quote(self, *, origin_chain_id: int, dest_chain_id: int,
              origin_currency: str, dest_currency: str, amount_in_raw: int,
              sender: str, recipient: str) -> BridgeQuote:
        """A bridge quote, or raise :class:`RelayError`.

        Every assertion below exists because the answer is a third party's and
        the funds are ours. A quote that came back for a different chain, a
        different recipient or a different asset than we asked for is not a
        worse quote — it is a DIFFERENT ORDER, and signing it would move value
        somewhere we did not choose.
        """
        if origin_chain_id == dest_chain_id:
            raise RelayError(
                f"origin and destination are the same chain ({origin_chain_id}); "
                f"that is a swap, not a bridge — use the swap verb")
        if amount_in_raw <= 0:
            raise RelayError(f"amount must be positive, got {amount_in_raw}")

        body = {
            "user": sender,
            "recipient": recipient,
            "originChainId": int(origin_chain_id),
            "destinationChainId": int(dest_chain_id),
            "originCurrency": origin_currency,
            "destinationCurrency": dest_currency,
            "amount": str(int(amount_in_raw)),
            "tradeType": "EXACT_INPUT",
        }
        try:
            raw = self._post(QUOTE_URL, body, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:
                pass
            raise RelayError(f"relay quote HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RelayError(f"relay quote failed: {type(exc).__name__}: {exc}") from exc

        return self._parse_quote(
            raw, origin_chain_id=origin_chain_id, dest_chain_id=dest_chain_id,
            origin_currency=origin_currency, dest_currency=dest_currency,
            amount_in_raw=amount_in_raw, sender=sender, recipient=recipient)

    def _parse_quote(self, raw: Dict[str, Any], *, origin_chain_id: int,
                     dest_chain_id: int, origin_currency: str, dest_currency: str,
                     amount_in_raw: int, sender: str, recipient: str) -> BridgeQuote:
        request_id = str(raw.get("requestId") or "").strip()
        if not request_id:
            raise RelayError("relay quote carries no requestId — "
                             "there would be no way to confirm arrival")

        details = raw.get("details") or {}
        cin = (details.get("currencyIn") or {})
        cout = (details.get("currencyOut") or {})
        cur_in = cin.get("currency") or {}
        cur_out = cout.get("currency") or {}

        got_origin = _int(cur_in.get("chainId"))
        got_dest = _int(cur_out.get("chainId"))
        if got_origin != int(origin_chain_id) or got_dest != int(dest_chain_id):
            raise RelayError(
                f"REFUSED: quote is for chains {got_origin}->{got_dest}, not the "
                f"{origin_chain_id}->{dest_chain_id} that was asked for")

        if not _same_address(cur_in.get("address"), origin_currency) or \
                not _same_address(cur_out.get("address"), dest_currency):
            raise RelayError(
                f"REFUSED: quote is for {cur_in.get('address')} -> "
                f"{cur_out.get('address')}, not the requested "
                f"{origin_currency} -> {dest_currency}")

        # The recipient is the whole point of a bridge. A quote that pays a
        # different address is the one failure mode that loses everything.
        if not _same_address(details.get("recipient"), recipient):
            raise RelayError(
                f"REFUSED: quote pays {details.get('recipient')}, not the "
                f"declared recipient {recipient}")
        if not _same_address(details.get("sender"), sender):
            raise RelayError(
                f"REFUSED: quote is sent by {details.get('sender')}, not {sender}")

        quoted_in = _int(cin.get("amount"))
        if quoted_in != int(amount_in_raw):
            raise RelayError(
                f"REFUSED: quote consumes {quoted_in}, not the declared "
                f"{amount_in_raw}")

        amount_out_raw = _int(cout.get("amount"))
        if amount_out_raw is None or amount_out_raw <= 0:
            raise RelayError(
                f"REFUSED: quote declares no positive output ({cout.get('amount')!r}). "
                f"An unreadable output cannot be asserted on arrival")

        dec_in = _int(cur_in.get("decimals"))
        dec_out = _int(cur_out.get("decimals"))
        if dec_in is None or dec_out is None:
            raise RelayError(
                "REFUSED: quote omits token decimals — an unknown denomination "
                "misprices the order by orders of magnitude")

        # The arrival floor. Relay states its own destination slippage allowance
        # in RAW units; the floor is the quoted output minus that. Unreadable
        # slippage is UNKNOWN, and an unknown floor refuses rather than
        # defaulting to 0 (a 0 floor asserts nothing at all on arrival).
        slip = ((details.get("slippageTolerance") or {}).get("destination") or {})
        slip_raw = _int(slip.get("value"))
        if slip_raw is None:
            raise RelayError(
                "REFUSED: quote states no destination slippage allowance, so "
                "there is no floor to assert the arrival against")
        min_out_raw = amount_out_raw - max(0, slip_raw)
        if min_out_raw <= 0:
            raise RelayError(
                f"REFUSED: the arrival floor computes to {min_out_raw} "
                f"(out {amount_out_raw} - slippage {slip_raw}); a non-positive "
                f"floor asserts nothing")

        steps = raw.get("steps") or []
        if not steps:
            raise RelayError("relay quote carries no steps — nothing to sign")
        if len(steps) != 1:
            # Multi-step means an approval leg or a chained order. Neither is
            # modelled by the two-phase guard yet, and guessing which step is
            # the value move is how funds go to the wrong place.
            raise RelayError(
                f"REFUSED: quote has {len(steps)} steps; only a single deposit "
                f"step is supported (an approval or chained leg is unmodelled)")
        step = steps[0]
        items = step.get("items") or []
        if len(items) != 1:
            raise RelayError(
                f"REFUSED: deposit step has {len(items)} items; expected exactly one")
        tx_data = items[0].get("data") or {}
        svm_origin = bool(tx_data.get("instructions"))
        if not svm_origin and not tx_data.get("to"):
            raise RelayError(
                "REFUSED: the deposit item carries neither SVM instructions nor "
                "an EVM `to` — there is nothing signable here")
        if not svm_origin:
            tx_chain = _int(tx_data.get("chainId"))
            if tx_chain != int(origin_chain_id):
                raise RelayError(
                    f"REFUSED: the transaction targets chain {tx_chain}, not the "
                    f"origin {origin_chain_id}")

        return BridgeQuote(
            request_id=request_id,
            origin_chain_id=int(origin_chain_id),
            dest_chain_id=int(dest_chain_id),
            sender=sender, recipient=recipient,
            currency_in=origin_currency, currency_out=dest_currency,
            amount_in_raw=int(amount_in_raw),
            amount_out_raw=int(amount_out_raw),
            min_out_raw=int(min_out_raw),
            decimals_in=int(dec_in), decimals_out=int(dec_out),
            symbol_in=str(cur_in.get("symbol") or "?"),
            symbol_out=str(cur_out.get("symbol") or "?"),
            amount_in_usd=_float(cin.get("amountUsd")),
            amount_out_usd=_float(cout.get("amountUsd")),
            impact_pct=_float((details.get("totalImpact") or {}).get("percent")),
            time_estimate_sec=_int(details.get("timeEstimate")),
            deposit_address=step.get("depositAddress"),
            tx_data=tx_data,
            svm_origin=svm_origin,
            raw=raw,
        )

    # -- status -----------------------------------------------------------

    def status(self, request_id: str) -> tuple:
        """``(state, detail)`` where state is success|pending|failure|unknown.

        `unknown` is a first-class answer, not an error: a status read that did
        not resolve must never be reported as either arrival or loss.
        """
        url = f"{STATUS_URL}?requestId={urllib.parse.quote(str(request_id))}"
        try:
            raw = self._get(url, timeout=self._timeout)
        except Exception as exc:
            return "unknown", f"status read failed: {type(exc).__name__}: {exc}"
        state = str(raw.get("status") or "").strip().lower()
        detail = str(raw.get("details") or raw.get("error") or state or "")[:300]
        if state in _SUCCESS:
            return "success", detail
        if state in _FAILURE:
            return "failure", detail
        if state in _PENDING:
            return "pending", detail
        return "unknown", detail or f"unrecognised status {state!r}"


def _same_address(a, b) -> bool:
    """Case-insensitive for EVM hex; exact for base58 — the ONE rule in
    ``core.wallet.addresses.same_address``."""
    from core.wallet.addresses import same_address
    return same_address(a, b)


# The bridge watcher lives in `core` (it is a wallet reconciliation loop) and may
# not import this module — so this module hands core a reader instead. Registered
# at import; `tools/__init__.py` imports this module at startup so the watcher has
# a reader in every process that loads the tool tier, not only after a bridge has
# already run. See core/wallet/bridge_status.py.
def _register_status_seam() -> None:
    try:
        from core.wallet.bridge_status import register_status_reader
        register_status_reader(lambda request_id: RelayBridgeProvider().status(request_id))
    except Exception:  # pragma: no cover - never let a seam break an import
        logger.debug("relay: status seam registration skipped", exc_info=True)


_register_status_seam()
