"""RealX402Client — wraps the official x402 Python client.

API surface verified and recorded in docs/superpowers/plans/notes/x402-client-api.md.
Lazy-imports the SDK so the module imports cleanly even when x402 is absent
(the tool is gated OFF by default).

SDK sequence (x402 2.13+, verified against installed 2.15.0 source):
  1. Build x402Client() — async payment-payload factory.
  2. Wrap signer: EthAccountSigner(signer.account) — exposes sign_typed_data + address.
  3. register_exact_evm_client(x402_client, sdk_signer, policies=[max_amount(...)]) —
     registers eip155:* + V1 networks AND installs the SDK's OWN amount-cap policy
     (`x402.max_amount`, filters candidate requirements by `get_amount()` in atomic
     units) so an over-cap requirement can never be SELECTED for payment in the
     first place — not just flagged after the fact (G-6/1).
  4. x402_client.on_before_payment_creation(...) — a fail-closed hook that ABORTS
     (raises PaymentAbortedError before any payload is created/signed — no money
     moves) when the SDK's own SELECTED requirement's network doesn't match our
     configured network. Reuses `_networks_match` so the SDK-level check and the
     probe-level check (below) share one definition (G-8).
     x402_client.on_after_payment_creation(...) — captures the exact requirement
     (network/pay_to/amount) we actually signed for, independent of any probe.
     This is what powers the no-probe path for non-idempotent methods (G-7) and
     the `paid`/`amount_usd` determination below (G-6/2).
  5. async with wrapHttpxWithPayment(x402_client) as http — intercepts 402, pays
     (subject to (3)/(4)), retries.
  6. http.request(method, url, content=body_bytes) — transparent auto-pay on 402.

Settlement success (Task 4 review Finding 1, G-6 scope): a resource server can
answer HTTP 200 with a decoded PAYMENT-RESPONSE settle header whose `success`
is False — on-chain settlement FAILED even though the HTTP round-trip looks
fine. `_reconcile_paid_amount` treats that as NOT a confirmed payment (`paid`
flips to False, nothing is recorded against the spend caps/audit ledger) and
logs a loud `logger.error` naming the tx/amount. Absent/unknown `success`
(no header, or a header that doesn't carry the field) keeps the amount but
marks it `amount_is_estimate=True` rather than authoritative.

quote() limitation: the x402 SDK has no price-only handshake without paying. quote()
performs a plain HEAD/GET probe and parses the 402 challenge header if the server
returns 402 immediately. This is best-effort: servers that don't return 402 on a
no-header request will return 200 and quote() returns None (free or no-402 endpoint).
The tool-side max_amount_usd guard is the operative cap; quote() is advisory only.

Non-idempotent requests (G-7): fetch_with_payment additionally runs a raw, unpaid
probe request BEFORE the SDK flow, but ONLY for idempotent methods (GET/HEAD/
OPTIONS) — cheap to repeat, and it gives an early, clear error (amount unreadable /
over cap / network mismatch) before any SDK traffic. For POST/PUT/PATCH/DELETE that
probe is skipped entirely: re-issuing a non-idempotent request as a throwaway probe
risks a second server-side side effect, so those methods rely SOLELY on the SDK's
own 402-discovery-then-paid-retry flow (step 5/6 above) — this module adds no extra
request beyond what wrapHttpxWithPayment itself issues. The SDK-native cap policy +
network-abort hook in (3)/(4) are what make this safe: they enforce the SAME
max_amount_usd/network guarantees the probe used to provide, but at the one point
(payment-payload creation) that every method — idempotent or not — passes through.

facilitator_url (G-9): the x402 CLIENT never chooses or contacts a facilitator —
verify/settle is the RESOURCE SERVER's concern (the server talks to a facilitator on
its own side). There is no facilitator_url parameter anywhere in x402Client,
register_exact_evm_client, or wrapHttpxWithPayment in the installed SDK.
fetch_with_payment therefore takes no facilitator_url argument.
X402_FACILITATOR_URL / WalletConfig.x402_facilitator_url remains valid — it is
RECEIVE-SIDE-ONLY configuration (for when POLYROB acts as an x402 resource server),
not something the paying client passes anywhere.

Task 4b (follow-up, pay-side live-payment blockers, fixed here): the Task 4 report
flagged three PRE-EXISTING defects that blocked any REAL on-chain payment from ever
pricing/passing the network check, none touched by G-6..G-12/Finding 1-2 above.
`_parse_402_challenge` now reads BOTH the V1 (`maxAmountRequired`) and V2 (`amount`)
challenge field names (version-detected via `x402Version`, with a best-effort
fallback when the marker is absent) and respects the challenge's own asset decimals
when present (default 6, USDC) — see `_challenge_decimals`. `_networks_match` now
maps our configured `"testnet"`/`"mainnet"` wallet mode to the exact set of V1
name/V2 CAIP-2 ids a real challenge may present for it (`_NETWORK_MODE_ALIASES`),
via exact string/set membership only — the prior `a in b or b in a` substring check
let `"eip155:8453"` (Base mainnet) collide with `"eip155:84532"` (Base Sepolia
testnet).

Task 4b review fix (CRITICAL, money-safety): decimals-awareness in
`_challenge_decimals` above landed ONLY in the advisory `quote()`/probe amount
display. The ACTUAL enforcement (`max_amount_atomic` handed to the SDK's
`max_amount` policy — the sole gate for POST/etc, since G-7 skips the probe for
non-idempotent methods) and accounting (`_capture_payment_info`'s signed-amount
capture, `_decode_settle_response`'s authoritative settled amount) paths still
hardcoded 6-decimal USDC via `_USDC_ATOMIC_PER_USD`. A malicious x402 resource
server naming a requirement in an ERC-20 with real on-chain decimals D<6 got an
atomic cap of `max_amount_usd * 10**6` regardless of D — a $50 cap could
authorize signing/transferring up to $50 * 10**(6-D) (at D=0, $50,000,000).
Trusting the challenge's own `decimals` field for the ENFORCEMENT path would
not have fixed this — an attacker controls that field too.

The fix is asset-pinning, not decimals-trusting: before treating any x402
requirement as payable, `_asset_is_canonical_usdc` verifies the requirement's
`asset` (the ERC-20 contract address the challenge names) EXACTLY matches the
canonical USDC contract for the active network (`_CANONICAL_USDC_ASSET`,
sourced from `core.wallet.onchain.USDC_BASE_MAINNET`/`USDC_BASE_SEPOLIA` — the
same addresses already trusted for the agent's own on-chain balance reads). A
non-matching asset is refused (fail-closed) at BOTH the probe layer (the early
GET/HEAD/OPTIONS check, alongside the existing network check) and the SDK
enforcement layer (`on_before_payment_creation`, so POST/no-probe methods are
covered too). With the asset pinned to canonical USDC, `_USDC_ATOMIC_PER_USD`'s
"6 decimals" is a guaranteed fact by construction, not an assumption —
`_challenge_decimals` remains scoped to the advisory quote()/probe-display path
only and is never consulted by the enforcement/accounting math.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
from core.wallet.signer import Signer
from tools.x402.client import X402Result

logger = logging.getLogger(__name__)

# RFC 7231 §4.2.2 non-idempotent methods, restricted to what x402 resources
# realistically use. G-7: fetch_with_payment must not add a redundant probe
# request for these — see the module docstring.
_NON_IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# USDC is 6 decimals — GUARANTEED here, not merely assumed, because every
# payable requirement is asset-pinned to the canonical USDC contract for our
# configured network (_asset_is_canonical_usdc) BEFORE this constant is used
# anywhere on the enforcement (max_amount_atomic → the SDK's max_amount
# policy) or accounting (_capture_payment_info, _decode_settle_response)
# paths. Do NOT "fix" this by trusting an untrusted challenge's own
# `decimals` field on those paths — an attacker controls that field too;
# asset-pinning is the actual gate. `_challenge_decimals` stays scoped to the
# advisory quote()/probe-amount-display path only (Task 4b review fix).
_USDC_ATOMIC_PER_USD = 1_000_000


class RealX402Client:
    """Production x402 client adapter wrapping the official x402 Python SDK.

    Lazy-imports the SDK on first construction so this module is importable
    without `x402` installed (the x402 tool is default-OFF / gated).
    """

    def __init__(self, policy=None) -> None:
        # Lazy import: verify the SDK is reachable at construction time so
        # callers get a clear ImportError rather than a late AttributeError.
        from x402 import x402Client  # noqa: F401 — verified present
        self._sdk_ready = True
        # C1 half 2 (2026-07-15): the wallet PolicyGate, threaded in by
        # service.py so the paying leg can re-check the SDK-selected
        # requirement's REAL amount before any payload is signed. The actual
        # payment is otherwise bounded ONLY by the agent-chosen max_amount_usd
        # (the SDK's own max_amount policy); a huge max_amount_usd would let a
        # small-looking advisory quote settle far above the wallet's own
        # catastrophic ceiling / daily cap. When None (a direct/legacy caller
        # that didn't thread the gate) the re-check layer is inert and the
        # service layer's worst-case gate is the sole protection — production
        # always threads it (see tools/x402/service.py::_get_client).
        self._policy = policy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_sdk_signer(signer: Signer):
        """Adapt a POLYROB Signer to the SDK's EthAccountSigner wrapper.

        EthAccountSigner(account) wraps any eth_account.LocalAccount and
        exposes sign_typed_data + address (the ClientEvmSigner protocol).
        The raw account is accessed via signer.account — in-process only.
        """
        from x402.mechanisms.evm.signers import EthAccountSigner
        return EthAccountSigner(signer.account)  # type: ignore[attr-defined]

    @staticmethod
    def _challenge_decimals(entry: dict) -> int:
        """Asset decimals for atomic→USD conversion (default 6, matching USDC).

        A resource server MAY carry the asset's decimals directly on the
        requirements entry (``decimals``) or nested under ``extra`` (the same
        dict the SDK itself uses for EIP-712 ``name``/``version`` domain data —
        see ``x402.mechanisms.evm.exact.server`` in the installed SDK). Checked
        defensively; any missing/unparseable value falls back to 6 rather than
        raising, so a challenge that simply omits decimals still prices fine.
        """
        raw = entry.get("decimals")
        if raw is None:
            extra = entry.get("extra")
            if isinstance(extra, dict):
                raw = extra.get("decimals")
        try:
            return int(raw) if raw is not None else 6
        except (TypeError, ValueError):
            return 6

    @staticmethod
    def _parse_402_challenge(response) -> Optional[dict]:
        """Parse a 402 PAYMENT-REQUIRED challenge → {amount, network, pay_to}.

        Returns None if the header is absent. Individual fields may be None if not
        present/parseable. ``amount`` is normalised to USD using the challenge's
        asset decimals if present, else 6 (USDC) — see ``_challenge_decimals``.

        V1 vs V2 field name: a V1 challenge (``x402Version: 1``, or no version
        marker — the SDK's own ``PaymentRequirementsV1``) carries the amount as
        ``maxAmountRequired``; a V2 challenge (``x402Version: 2`` — the SDK's
        current/default ``PaymentRequirements``) carries it as ``amount``
        (``BaseX402Model``'s ``to_camel`` alias generator leaves a single-word
        field unchanged — verified against the installed x402==2.15.0 source,
        ``x402/schemas/payments.py`` vs ``x402/schemas/v1.py``). Reading only
        ``maxAmountRequired`` (the old, PRE-EXISTING bug) left every real V2
        challenge unpriced — ``quote()``/the probe fail closed (amount=None) on
        every standards-compliant V2 server. When the version marker is
        absent/unrecognised, both field names are tried (V1 name first, for
        legacy compat) rather than refusing outright.
        """
        try:
            import base64
            import json

            header = (
                response.headers.get("PAYMENT-REQUIRED")
                or response.headers.get("payment-required")
                or response.headers.get("X-PAYMENT-REQUIRED")
            )
            if not header:
                return None
            decoded = json.loads(base64.b64decode(header + "=="))
            accepts = decoded.get("accepts", [])
            entry = accepts[0] if accepts else decoded  # V2 list, or V1 flat
            version = decoded.get("x402Version")
            # Minor (Task 4 review): tolerate a stringy "1"/"2" version marker
            # (e.g. a server that serializes it as JSON string, not int) —
            # normalize before the equality checks below rather than falling
            # through to the try-both-fields branch on a technicality.
            try:
                version = int(version) if version is not None else None
            except (TypeError, ValueError):
                version = None
            if version == 1:
                raw = entry.get("maxAmountRequired")
            elif version == 2:
                raw = entry.get("amount")
            else:
                # Absent/unrecognised version marker: best-effort — try both
                # field names rather than refusing outright. V1 name checked
                # first (legacy-compat convention — this is the field the
                # ORIGINAL/pre-Task-4b parser read exclusively, so an absent
                # marker keeps that behavior as the primary path); falls back
                # to the V2 name. Ordering is cosmetic: a real, standards-
                # compliant server carries exactly one of the two, so at most
                # one `.get()` here ever returns non-None. A challenge that
                # matches neither (and isn't unparseable JSON) legitimately
                # fails closed below (amount stays None).
                raw = entry.get("maxAmountRequired")
                if raw is None:
                    raw = entry.get("amount")
            decimals = RealX402Client._challenge_decimals(entry)
            amount = (float(raw) / (10 ** decimals)) if raw is not None else None
            network = entry.get("network") or decoded.get("network")
            pay_to = entry.get("payTo") or entry.get("pay_to") or decoded.get("payTo")
            asset = entry.get("asset") or decoded.get("asset")
            return {"amount": amount, "network": network, "pay_to": pay_to, "asset": asset}
        except Exception as exc:  # noqa: BLE001
            logger.debug("quote: could not parse 402 header: %s", exc)
            # Distinguish "present but unparseable" from "absent": a present-but-broken
            # challenge must NOT be treated as free/None by the cap check (fail-closed).
            return {"amount": None, "network": None, "pay_to": None, "asset": None,
                    "_unparseable": True}

    @classmethod
    def _parse_402_amount(cls, response) -> Optional[float]:
        """Best-effort: required USD amount from a 402 response (None if absent)."""
        ch = cls._parse_402_challenge(response)
        return ch.get("amount") if ch else None

    # `core/wallet/config.py::WalletConfig.network` is always exactly "testnet"
    # or "mainnet" — never a raw chain id — but real x402 challenges carry
    # actual network identifiers: V1 legacy names ("base", "base-sepolia") or
    # V2 CAIP-2 ids ("eip155:8453", "eip155:84532"; verified against
    # x402==2.15.0's own NETWORK_CONFIGS/V1_NETWORK_CHAIN_IDS tables in
    # x402.mechanisms.evm.constants / .v1.constants — Base mainnet=8453,
    # Base Sepolia testnet=84532). This is the ONE mapping from our configured
    # mode to every network id a real challenge may legitimately present for it.
    # Base only (the wallet's one supported chain today); extend here if
    # another chain is ever supported.
    _NETWORK_MODE_ALIASES = {
        "testnet": frozenset({"base-sepolia", "eip155:84532"}),
        "mainnet": frozenset({"base", "eip155:8453"}),
    }

    # Canonical USDC ERC-20 contract address for each network identity above —
    # the asset-pin gate (Task 4b review fix, CRITICAL — see the module
    # docstring and `_asset_is_canonical_usdc`). Keyed by every string
    # `_NETWORK_MODE_ALIASES` accepts for a mode (the mode name itself, the V1
    # name, the V2 CAIP-2 id), built FROM that table so the two can never drift
    # apart. Values are `core.wallet.onchain`'s named constants — the SAME
    # addresses already trusted for the agent's own on-chain USDC balance
    # reads, not a second hand-typed copy.
    _CANONICAL_USDC_ASSET = {
        "testnet": USDC_BASE_SEPOLIA, "mainnet": USDC_BASE_MAINNET,
        **{alias: USDC_BASE_SEPOLIA for alias in _NETWORK_MODE_ALIASES["testnet"]},
        **{alias: USDC_BASE_MAINNET for alias in _NETWORK_MODE_ALIASES["mainnet"]},
    }

    @staticmethod
    def _asset_is_canonical_usdc(configured: Optional[str], asset: Optional[str]) -> bool:
        """Asset-pin gate (Task 4b review fix, CRITICAL — money safety).

        True only when `asset` (the ERC-20 contract address an x402 challenge
        names as its payment token) is EXACTLY the canonical USDC contract for
        OUR configured network. This is what makes `_USDC_ATOMIC_PER_USD`'s
        hardcoded 6 decimals correct BY CONSTRUCTION rather than an assumption:
        a malicious resource server naming a requirement in some OTHER ERC-20
        — especially one with real on-chain decimals D<6 — would otherwise get
        an atomic cap computed as `max_amount_usd * 10**6` regardless of D (at
        D=0, a $50 cap would authorize signing/transferring up to $50,000,000
        of that token). Trusting the challenge's own `decimals` field on the
        enforcement path would NOT fix this — an attacker controls that field
        too (see `_challenge_decimals`, deliberately unused here); pinning the
        asset closes the gap at the source instead.

        Fail-closed in every ambiguous case — no configured network, no asset
        on the challenge, or a configured value we hold no canonical asset
        for — returns False (refuse), never True (allow) by omission.
        """
        if not configured or not asset:
            return False
        canonical = RealX402Client._CANONICAL_USDC_ASSET.get(str(configured).strip().lower())
        if canonical is None:
            return False
        return str(asset).strip().lower() == canonical.lower()

    @staticmethod
    def _networks_match(configured: Optional[str], challenged: Optional[str]) -> bool:
        """Network equivalence between OUR configured wallet mode and a
        challenge's network id, via EXACT string/set membership only — never
        substring/prefix matching.

        Fail-closed (G-8): once OUR side has a configured network, an absent/blank
        challenge network no longer waves the check through. A compliant paid
        resource always names its network in the challenge; an omitted one is
        treated as a mismatch, not as "nothing to compare" — the old behaviour let
        a challenge that simply left the field out bypass chain binding entirely.

        `configured` is normally "testnet"/"mainnet" (the only two values
        `WalletConfig.network` can hold) — matched via `_NETWORK_MODE_ALIASES`
        against BOTH the V1 name and V2 CAIP-2 id for that mode. When
        `configured` isn't a recognised mode (e.g. a raw network id passed
        directly), this falls back to exact string equality. The PRE-EXISTING
        `a in b or b in a` substring check this replaces let
        "eip155:8453" (Base mainnet) match "eip155:84532" (Base Sepolia
        testnet) — a real numeric chain-id prefix collision — which is now
        impossible: neither alias set contains the other's id, and the
        fallback is exact equality only.
        """
        if not configured:
            return True  # nothing on our side to enforce against
        if not challenged:
            return False  # configured, but the challenge is silent → fail closed
        a = str(configured).strip().lower()
        b = str(challenged).strip().lower()
        aliases = RealX402Client._NETWORK_MODE_ALIASES.get(a)
        if aliases is not None:
            return b in aliases
        return a == b

    @staticmethod
    def _decode_settle_response(response) -> Optional[dict]:
        """Decode the PAYMENT-RESPONSE / X-PAYMENT-RESPONSE header (base64 JSON
        SettleResponse, via the x402 SDK's own `decode_payment_response_header`)
        into the ACTUAL settled amount/network/payer/tx (G-6/2).

        Returns None if the header is absent or fails to decode; callers must then
        fall back to a pre-settlement estimate and mark it as such.
        """
        header = (
            response.headers.get("PAYMENT-RESPONSE")
            or response.headers.get("payment-response")
            or response.headers.get("X-PAYMENT-RESPONSE")
            or response.headers.get("x-payment-response")
        )
        if not header:
            return None
        try:
            from x402.http.utils import decode_payment_response_header
            sr = decode_payment_response_header(header)
            amount = (
                float(sr.amount) / _USDC_ATOMIC_PER_USD if sr.amount is not None else None
            )
            return {
                "amount": amount,
                "network": sr.network,
                "payer": sr.payer,
                "tx_hash": sr.transaction,
                "success": sr.success,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not decode x402 settlement response header: %s", exc)
            return None

    @staticmethod
    def _reconcile_paid_amount(
        *,
        paid: bool,
        settle: Optional[dict],
        payment_info: dict,
        probe_amount: Optional[float],
        max_amount_usd: float,
        url: str,
    ) -> tuple:
        """Decide the final (paid, amount_paid, amount_is_estimate) from the SDK's
        payment-creation outcome + the decoded PAYMENT-RESPONSE settle header.

        Pure decision logic — no SDK/network dependency — so it is directly
        unit-testable without the x402 SDK installed (mirrors `_parse_402_challenge`
        / `_networks_match`).

        Task 4 review Finding 1 (G-6 scope): a resource server can answer HTTP 200
        with a settle response whose ``success`` is False — on-chain settlement
        FAILED even though the HTTP layer looks fine. That must NOT be treated as
        a confirmed payment (it would corrupt the audit ledger and consume the
        daily/venue spend cap on money that never settled): flip `paid` to False,
        zero the amount, and log loudly naming the tx/amount. When `success` is
        absent/unknown (older/absent header, or a header that doesn't carry the
        field) the existing "paid, but estimate" behaviour is kept.
        """
        if not paid:
            return False, 0.0, False

        settle_success = settle.get("success") if settle else None
        if settle_success is False:
            logger.error(
                "x402: settlement FAILED for %s — resource server answered "
                "non-402 but PAYMENT-RESPONSE reports success=False "
                "(tx=%s, signed amount=$%.6f); NOT recording as a confirmed payment",
                url, settle.get("tx_hash"), payment_info.get("amount") or 0.0,
            )
            return False, 0.0, False

        amount_is_estimate = False
        if settle and settle.get("amount") is not None:
            amount_paid = settle["amount"]
            # success present but not explicitly True (e.g. None/missing field on
            # an older header) → we can't confirm settlement, so the header amount
            # is not gospel: mark it an estimate rather than authoritative.
            amount_is_estimate = settle_success is None
            if probe_amount is not None and amount_paid > probe_amount + 1e-9:
                logger.error(
                    "x402: settled amount $%.6f for %s exceeds the pre-pay probe "
                    "estimate $%.6f (server raised the price between probe and pay)",
                    amount_paid, url, probe_amount,
                )
        elif payment_info.get("amount") is not None:
            amount_paid = payment_info["amount"]
            amount_is_estimate = True
        else:
            amount_paid = probe_amount if probe_amount is not None else 0.0
            amount_is_estimate = True

        if amount_paid > max_amount_usd:
            # Should be unreachable — the SDK-native cap policy refuses to even
            # build the payload for an over-cap requirement. If this fires anyway
            # (e.g. a facilitator settled a different amount than what was
            # signed), money has ALREADY moved: surface it loudly, never swallow.
            logger.error(
                "x402: SETTLED amount $%.6f for %s exceeds authorized "
                "max_amount_usd=$%.6f — money already moved, investigate",
                amount_paid, url, max_amount_usd,
            )
        return True, amount_paid, amount_is_estimate

    @staticmethod
    def _policy_recheck_reason(policy, real_amount_usd: Optional[float]) -> Optional[str]:
        """C1 half 2: re-run the wallet PolicyGate against the ACTUAL amount the
        SDK selected to pay, at the one point before any payload is signed.

        Pure decision logic — no SDK/network dependency — so it is directly
        unit-testable without the x402 SDK installed (mirrors
        `_reconcile_paid_amount` / `_networks_match`). Returns None when the
        spend is allowed, else a human-readable abort reason (the caller turns a
        non-None reason into an SDK `AbortResult`, so no payload is created and
        no money moves).

        Why this exists on TOP of the service-layer worst-case gate (half 1):
        the paying leg is bounded only by the agent-chosen `max_amount_usd` (the
        SDK's own `max_amount` policy). Putting the wallet's authoritative gate
        (catastrophic per-tx ceiling + rolling-24h daily/venue caps) on the
        REAL selected amount — with FRESH gate state, at sign time — makes the
        actual spend structurally gated regardless of how large `max_amount_usd`
        was, and also closes the honest-server raise-price-between-quote-and-pay
        variant.

        Fail-CLOSED in every ambiguous case (per the money-path contract): a
        None amount (the client couldn't read the selected requirement's real
        amount) or a raising/erroring gate returns a refusal reason — a money
        path must NEVER sign an amount the gate couldn't evaluate. `venue`
        matches the service layer's `"x402"` so per-venue caps apply;
        `idempotency_key=None` keeps this a pure amount/cap gate (the replay
        guard is the service layer's job, and `record()` — after an actual
        payment — is what adds the key to the seen-set)."""
        if real_amount_usd is None:
            return (
                "x402: could not determine the SDK-selected requirement's real "
                "amount for the wallet policy re-check; refusing to pay "
                "(fail-closed)"
            )
        try:
            decision = policy.check(
                venue="x402", amount_usd=real_amount_usd, idempotency_key=None
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"x402: wallet policy re-check raised ({exc}) for real amount "
                f"${real_amount_usd:.6f}; refusing to pay (fail-closed)"
            )
        if not decision.allowed:
            return (
                f"x402: real payment amount ${real_amount_usd:.6f} blocked by "
                f"wallet policy: {decision.reason}"
            )
        return None

    # ------------------------------------------------------------------
    # X402PaymentClient interface
    # ------------------------------------------------------------------

    async def quote(self, url: str) -> Optional[float]:
        """Best-effort price probe: return the required USD amount or None.

        Sends a plain GET without an X-PAYMENT header. If the server
        responds with 402, we parse the PAYMENT-REQUIRED challenge header to
        extract the amount. Returns None if the endpoint is free (200), the
        server doesn't honour no-header 402, or the header is unparseable.

        NOTE: The x402 SDK has no dedicated price-only handshake. This
        approach works for compliant servers but is advisory only — the
        max_amount_usd guard in fetch_with_payment is the operative cap.
        """
        try:
            import httpx
            async with httpx.AsyncClient() as http:
                resp = await http.get(url)
                if resp.status_code == 402:
                    return self._parse_402_amount(resp)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("quote: probe failed for %s: %s", url, exc)
            return None

    async def fetch_with_payment(
        self,
        *,
        url: str,
        method: str,
        body: Optional[str],
        signer: Signer,
        network: str,
        max_amount_usd: float,
    ) -> X402Result:
        """Perform an auto-paying HTTP request via the x402 SDK.

        See the module docstring for the full sequence and the G-6/G-7/G-8/G-9
        design this implements. Summary:
          - amount is hard-capped on the PAYING leg via the SDK's own
            `max_amount` policy (G-6/1), not just on our probe;
          - network is hard-bound on the PAYING leg via a fail-closed
            `on_before_payment_creation` hook (G-8), reusing `_networks_match`;
          - the requirement's ASSET is hard-pinned to the canonical USDC
            contract for the configured network, at BOTH the probe layer and
            the same `on_before_payment_creation` hook (Task 4b review fix,
            CRITICAL) — see `_asset_is_canonical_usdc`. This is what makes the
            hardcoded 6-decimal `_USDC_ATOMIC_PER_USD` math correct by
            construction rather than trusting an attacker-controlled
            `decimals` field;
          - the recorded `X402Result.amount_usd` is, in priority order: the
            ACTUAL settled amount decoded from X-PAYMENT-RESPONSE, else the
            exact amount we signed for (captured via `on_after_payment_creation`),
            else the pre-pay probe estimate — with `amount_is_estimate` marking
            anything short of the first (G-6/2);
          - for POST/PUT/PATCH/DELETE no separate probe request is issued
            (G-7) — GET/HEAD/OPTIONS keep the cheap early-fail probe.
        """
        from x402 import x402Client, AbortResult, max_amount
        from x402.mechanisms.evm.exact import register_exact_evm_client
        from x402.mechanisms.evm.signers import EthAccountSigner
        from x402.http.clients.httpx import wrapHttpxWithPayment

        sdk_signer = EthAccountSigner(signer.account)  # type: ignore[attr-defined]

        body_bytes = body.encode() if body else None
        method_upper = (method or "GET").upper()
        run_probe = method_upper not in _NON_IDEMPOTENT_METHODS

        # Optional early probe (G-7: idempotent methods only — safe to repeat).
        # Intercepts the 402 challenge to fail fast + fail closed before any SDK
        # traffic. Skipped for non-idempotent methods: the SDK-native cap/network
        # enforcement below (max_amount policy + on_before_payment_creation hook)
        # covers the same ground on the one request the SDK itself has to make.
        probe_amount: Optional[float] = None
        probe_pay_to: Optional[str] = None
        if run_probe:
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient() as probe_client:
                    probe_kwargs: dict = {"method": method, "url": url}
                    if body_bytes:
                        probe_kwargs["content"] = body_bytes
                    probe_resp = await probe_client.request(**probe_kwargs)
                    if probe_resp.status_code == 402:
                        challenge = self._parse_402_challenge(probe_resp) or {}
                        probe_amount = challenge.get("amount")
                        probe_pay_to = challenge.get("pay_to")
                        # H2 fail-CLOSED: if a payment is required but we can't read
                        # the amount, refuse rather than auto-pay an unknown sum.
                        if probe_amount is None:
                            raise ValueError(
                                f"x402: 402 challenge for {url} had no readable amount; "
                                f"refusing to auto-pay an unknown amount (fail-closed)"
                            )
                        if probe_amount > max_amount_usd:
                            raise ValueError(
                                f"x402: required amount ${probe_amount:.6f} exceeds "
                                f"max_amount_usd=${max_amount_usd:.6f} for {url}"
                            )
                        # H1: bind to the configured network — reject a challenge that
                        # demands a different chain than the operator configured.
                        challenged_net = challenge.get("network")
                        if not self._networks_match(network, challenged_net):
                            raise ValueError(
                                f"x402: challenge network '{challenged_net}' != configured "
                                f"'{network}' for {url}; refusing to pay (fail-closed)"
                            )
                        # Asset-pin (Task 4b review fix, CRITICAL): refuse unless the
                        # requirement's ERC-20 asset is EXACTLY the canonical USDC
                        # contract for our configured network. This is the actual
                        # money-safety gate — do NOT rely on the challenge's own
                        # `decimals` field for enforcement (attacker-controlled); see
                        # `_asset_is_canonical_usdc` and the module docstring.
                        challenged_asset = challenge.get("asset")
                        if not self._asset_is_canonical_usdc(network, challenged_asset):
                            raise ValueError(
                                f"x402: challenge asset '{challenged_asset}' for {url} is "
                                f"not the canonical USDC contract for network '{network}'; "
                                f"refusing to pay (fail-closed)"
                            )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A probe transport failure means we cannot verify the cap/network.
                # Fail-closed: do NOT proceed to a blind auto-pay.
                raise ValueError(
                    f"x402: could not verify payment terms for {url} ({exc}); "
                    f"refusing to auto-pay (fail-closed)"
                ) from exc

        # SDK-native enforcement on the PAYING leg itself (G-6/G-8 + Task 4b review
        # fix): a cap policy that makes an over-cap requirement unselectable, and a
        # before-creation hook that aborts (no payload created, no money moved) on
        # a network OR asset the SDK itself selected but that doesn't match our
        # configuration/canonical-USDC pin. This runs for every method — it is what
        # lets G-7 skip the probe above safely, and it closes the probe-vs-pay
        # TOCTOU for methods that DO keep the probe (a server that raises the
        # price, swaps networks, or swaps the payable asset between our probe and
        # the SDK's own 402-discovery can no longer slip a bad payment through).
        # `max_amount_atomic` below is correct-by-construction 6-decimal math ONLY
        # because `_abort_if_invalid_requirement` guarantees the asset is USDC
        # before any payload is ever created — see `_USDC_ATOMIC_PER_USD`'s comment.
        max_amount_atomic = int(round(max_amount_usd * _USDC_ATOMIC_PER_USD))
        x402_c = x402Client()
        register_exact_evm_client(x402_c, sdk_signer, policies=[max_amount(max_amount_atomic)])

        payment_info: dict = {"happened": False, "amount": None, "pay_to": None}

        def _abort_if_invalid_requirement(ctx):
            selected = ctx.selected_requirements
            selected_network = getattr(selected, "network", None)
            if not self._networks_match(network, selected_network):
                return AbortResult(
                    reason=(
                        f"x402: SDK-selected network '{selected_network}' != configured "
                        f"'{network}' for {url}; refusing to pay (fail-closed)"
                    )
                )
            # Asset-pin (Task 4b review fix, CRITICAL): the SDK-level mirror of the
            # probe-layer check above. This is the SOLE enforcement point for
            # POST/PUT/PATCH/DELETE (G-7 skips the probe for those) and closes the
            # probe-vs-pay TOCTOU for GET/HEAD/OPTIONS too.
            selected_asset = getattr(selected, "asset", None)
            if not self._asset_is_canonical_usdc(network, selected_asset):
                return AbortResult(
                    reason=(
                        f"x402: SDK-selected asset '{selected_asset}' for {url} is not "
                        f"the canonical USDC contract for network '{network}'; refusing "
                        f"to pay (fail-closed)"
                    )
                )
            # C1 half 2 (2026-07-15): re-run the wallet PolicyGate against the
            # REAL amount the SDK selected, before any payload is signed. Runs
            # AFTER the asset-pin above so `_USDC_ATOMIC_PER_USD`'s 6-decimal
            # math is correct-by-construction (the requirement is guaranteed
            # canonical USDC by this point). The paying leg is otherwise bounded
            # only by max_amount_usd (the SDK's max_amount policy) — this puts
            # the wallet's catastrophic ceiling + daily/venue caps directly on
            # the actual spend. Inert when no policy was threaded in (legacy /
            # direct callers); production always threads it via service.py.
            if self._policy is not None:
                try:
                    selected_amount_usd = float(selected.get_amount()) / _USDC_ATOMIC_PER_USD
                except Exception:  # noqa: BLE001 — unreadable amount → fail closed below
                    selected_amount_usd = None
                reason = self._policy_recheck_reason(self._policy, selected_amount_usd)
                if reason is not None:
                    return AbortResult(reason=reason)
            return None

        def _capture_payment_info(ctx):
            payment_info["happened"] = True
            req = ctx.selected_requirements
            payment_info["pay_to"] = getattr(req, "pay_to", None)
            try:
                payment_info["amount"] = float(req.get_amount()) / _USDC_ATOMIC_PER_USD
            except Exception:  # noqa: BLE001
                pass

        x402_c.on_before_payment_creation(_abort_if_invalid_requirement)
        x402_c.on_after_payment_creation(_capture_payment_info)

        # Auto-pay client: intercepts 402, signs + attaches X-PAYMENT, retries.
        async with wrapHttpxWithPayment(x402_c) as http:
            req_kwargs: dict = {"method": method, "url": url}
            if body_bytes:
                req_kwargs["content"] = body_bytes
            response = await http.request(**req_kwargs)

        # paid=True only when the SDK actually created+signed a payment payload
        # AND the retried response is no longer a 402 (a created-but-rejected
        # payment would still show 402 — never record a phantom paid entry for
        # that). This replaces the old probe-status heuristic with a definitive
        # SDK-sourced signal that works whether or not a probe ran (G-6/G-7).
        paid = bool(payment_info["happened"] and response.status_code != 402)

        settle = self._decode_settle_response(response) if paid else None
        paid, amount_paid, amount_is_estimate = self._reconcile_paid_amount(
            paid=paid,
            settle=settle,
            payment_info=payment_info,
            probe_amount=probe_amount,
            max_amount_usd=max_amount_usd,
            url=url,
        )

        tx_hash: Optional[str] = settle.get("tx_hash") if settle else None
        pay_to: Optional[str] = (
            payment_info["pay_to"]
            or response.headers.get("X-PAY-TO")
            or response.headers.get("x-pay-to")
            or probe_pay_to
        )

        return X402Result(
            body=response.text,
            paid=paid,
            amount_usd=amount_paid or 0.0,
            tx_hash=tx_hash or None,
            pay_to=pay_to or None,
            status_code=response.status_code,
            amount_is_estimate=amount_is_estimate,
        )
