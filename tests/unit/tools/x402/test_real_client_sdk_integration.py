"""End-to-end RealX402Client.fetch_with_payment coverage against the REAL x402
SDK (skipped when the SDK isn't installed — it is default-OFF/gated, see
requirements.txt "x402>=2.13.0"). Drives a fake x402 resource server built on
httpx's transport layer so no network access is required.

These scenarios were interactively verified against the installed x402==2.15.0
source (see the Task 4 report) before being committed here; they exercise the
SAME SDK mechanisms real_client.py relies on (x402Client.register_policy via
`max_amount`, on_before_payment_creation/on_after_payment_creation hooks,
decode_payment_response_header) rather than re-mocking the SDK's internals.

Covers G-6 (actual settled amount reconciliation + estimate marking),
G-7 (no extra probe request for non-idempotent methods), and G-8 (SDK-level
network fail-closed, in addition to the pure _networks_match unit tests in
test_real_client_smoke.py).
"""
import base64
import importlib.util
import json

import pytest

x402_installed = importlib.util.find_spec("x402") is not None

pytestmark = pytest.mark.skipif(not x402_installed, reason="x402 SDK not installed in this env")


if x402_installed:
    import httpx
    from eth_account import Account

    from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
    from core.wallet.signer import LocalEoaSigner
    from tools.x402.real_client import RealX402Client
    from x402.http.utils import encode_payment_response_header
    from x402.schemas import PaymentRequired, PaymentRequirements
    from x402.schemas.responses import SettleResponse

    NETWORK = "eip155:84532"  # base-sepolia — has known EIP-712 asset config in the SDK
    # Genuine Base Sepolia USDC — matches core.wallet.onchain.USDC_BASE_SEPOLIA
    # (also verified directly against x402==2.15.0's own
    # x402.mechanisms.evm.v1.constants/.constants default-asset tables).
    ASSET = USDC_BASE_SEPOLIA
    # Task 4b review fix (CRITICAL): the genuine Base MAINNET USDC contract —
    # used by the "mainnet mode succeeds" test below, which must supply the
    # correct-for-network asset now that fetch_with_payment asset-pins.
    ASSET_MAINNET = USDC_BASE_MAINNET
    # Some OTHER ERC-20 — not USDC on either network. Used to prove the
    # asset-pin gate refuses a non-canonical asset regardless of amount/network.
    NOT_USDC_ASSET = "0x000000000000000000000000000000deadbeef"
    PAY_TO = Account.from_key(b"\x22" * 32).address
    KEY = b"\x11" * 32

    def _requirements(amount_atomic: str, network: str = NETWORK, asset: str = ASSET,
                       extra: dict = None) -> "PaymentRequired":
        return PaymentRequired(
            x402_version=2,
            accepts=[
                PaymentRequirements(
                    scheme="exact", network=network, asset=asset, amount=amount_atomic,
                    pay_to=PAY_TO, max_timeout_seconds=60,
                    extra=extra if extra is not None else {"name": "USDC", "version": "2"},
                )
            ],
        )

    def _encode_challenge(pr: "PaymentRequired", *, legacy_alias: bool = True) -> str:
        """Encode a V2 challenge the SDK can parse.

        `legacy_alias=True` (default, preserves every pre-Task-4b test
        byte-identical) also injects a `maxAmountRequired` alias key (harmless —
        Pydantic ignores unknown fields) alongside the genuine V2 `amount`
        field, so these tests don't depend on which field name the parser
        reads. `legacy_alias=False` emits the WIRE-GENUINE V2 shape only
        (`amount`, no `maxAmountRequired`) — this is what Task 4b's new tests
        below use to prove `_parse_402_challenge` now actually reads the real
        V2 field (the PRE-EXISTING bug flagged in the Task 4 report: reading
        only the V1 field name left every real V2 challenge unpriced).
        """
        data = json.loads(pr.model_dump_json(by_alias=True, exclude_none=True))
        if legacy_alias:
            data["accepts"][0]["maxAmountRequired"] = data["accepts"][0]["amount"]
        return base64.b64encode(json.dumps(data).encode()).decode()

    class FakeServer:
        """Records every request it sees; can declare one price and settle a
        different one (models a server that raises its price)."""

        def __init__(self, challenge_amount: str, settle_amount: str, network: str = NETWORK,
                     asset: str = ASSET, extra: dict = None):
            self.requests = []
            self.challenge_amount = challenge_amount
            self.settle_amount = settle_amount
            self.network = network
            # Task 4b review fix: the challenge's ERC-20 asset — defaults to the
            # genuine Base Sepolia USDC (matches the module default NETWORK).
            # Override to test the asset-pin gate (a non-USDC asset, or the
            # wrong-network USDC).
            self.asset = asset
            self.extra = extra
            self.omit_challenge_network = False
            self.omit_settle_header = False
            self.settle_success = True
            # Task 4b: True (default) keeps every pre-existing test's challenge
            # byte-identical (both field names present). False emits a
            # WIRE-GENUINE V2 challenge (`amount` only) — what a real,
            # standards-compliant V2 x402 resource server actually sends.
            self.legacy_alias = True

        def handler(self, request: "httpx.Request") -> "httpx.Response":
            self.requests.append((request.method, str(request.url), bool(request.content)))
            paid_header = (
                request.headers.get("payment-signature") or request.headers.get("x-payment")
            )
            if not paid_header:
                pr = _requirements(self.challenge_amount, self.network, self.asset, self.extra)
                data = json.loads(pr.model_dump_json(by_alias=True, exclude_none=True))
                if self.legacy_alias:
                    data["accepts"][0]["maxAmountRequired"] = data["accepts"][0]["amount"]
                if self.omit_challenge_network:
                    del data["accepts"][0]["network"]
                enc = base64.b64encode(json.dumps(data).encode()).decode()
                return httpx.Response(402, headers={"PAYMENT-REQUIRED": enc},
                                      json={"error": "payment required"})
            if self.omit_settle_header:
                return httpx.Response(200, text="SECRET-DATA")
            sr = SettleResponse(success=self.settle_success, payer="0xPAYER", transaction="0xTXHASH",
                                network=self.network, amount=self.settle_amount)
            return httpx.Response(200, headers={"PAYMENT-RESPONSE": encode_payment_response_header(sr)},
                                  text="SECRET-DATA")


@pytest.fixture
def fake_transport(monkeypatch):
    """Route every httpx.AsyncHTTPTransport instance (our probe's plain
    AsyncClient() AND the SDK's x402AsyncTransport internal fallback
    transport) through a FakeServer.

    IMPORTANT: patching httpx.AsyncClient.__init__(transport=...) instead would
    silently REPLACE x402AsyncTransport itself (its whole 402-retry wrapper),
    defeating the SDK's own retry logic — this patches one level lower, the
    method every default-constructed AsyncHTTPTransport shares, which the SDK
    still reaches through its wrapper.
    """
    server = FakeServer(challenge_amount="50000", settle_amount="50000")

    async def patched(self, request):
        resp = server.handler(request)
        await resp.aread()
        return resp

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", patched)
    return server


@pytest.mark.asyncio
async def test_g6_settled_amount_reconciled_and_not_marked_estimate(fake_transport):
    """G-6: server raises the price between our probe and the SDK's own paid
    retry. The recorded amount must be the ACTUAL settled amount decoded from
    X-PAYMENT-RESPONSE (not the stale probe estimate), and must NOT be marked
    an estimate."""
    fake_transport.challenge_amount = "40000"  # probe sees $0.04
    fake_transport.settle_amount = "60000"     # actually settles at $0.06

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
    )
    assert res.paid is True
    assert res.amount_usd == pytest.approx(0.06)
    assert res.amount_is_estimate is False
    assert res.tx_hash == "0xTXHASH"
    assert res.pay_to == PAY_TO


@pytest.mark.asyncio
async def test_g6_no_settle_header_falls_back_to_estimate(fake_transport):
    """G-6: when the paid response has no PAYMENT-RESPONSE header at all, fall
    back to the exact amount we signed for and mark it as an estimate (not the
    unconditionally-authoritative settled amount)."""
    fake_transport.challenge_amount = "30000"
    fake_transport.settle_amount = "30000"
    fake_transport.omit_settle_header = True

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
    )
    assert res.paid is True
    assert res.amount_usd == pytest.approx(0.03)
    assert res.amount_is_estimate is True
    assert res.tx_hash is None


@pytest.mark.asyncio
async def test_g6_settle_success_false_is_not_a_confirmed_payment(fake_transport, caplog):
    """Task 4 review Finding 1 (G-6 scope): the resource server answers HTTP
    200 (not 402) but the decoded PAYMENT-RESPONSE reports success=False —
    on-chain settlement FAILED. This must NOT be treated as a paid fetch: no
    phantom `paid=True` entry should ever reach the wallet's spend caps/audit
    ledger, and the failure must be logged loudly (naming the tx)."""
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.settle_success = False

    client = RealX402Client()
    with caplog.at_level("ERROR"):
        res = await client.fetch_with_payment(
            url="http://fake/paid", method="GET", body=None,
            signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
        )
    assert res.paid is False
    assert res.amount_usd == 0.0
    assert any("settlement FAILED" in r.message and "0xTXHASH" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_g6_sdk_cap_refuses_above_cap_challenge(fake_transport):
    """G-6/1: the paying leg itself (not just our probe) refuses a challenge
    over max_amount_usd, via the SDK's own `max_amount` policy."""
    fake_transport.challenge_amount = "999999999"  # ~$1000, way over cap
    fake_transport.settle_amount = "999999999"

    client = RealX402Client()
    with pytest.raises(Exception):
        await client.fetch_with_payment(
            url="http://fake/paid", method="GET", body=None,
            signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
        )


@pytest.mark.asyncio
async def test_g7_non_idempotent_method_issues_no_extra_probe(fake_transport):
    """G-7: for POST/PUT/PATCH/DELETE, real_client.py must not add a request
    beyond what the SDK's own 402-discovery + paid-retry flow issues (2 total:
    the unpaid discovery attempt and the paid retry) — no separate probe."""
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="POST", body='{"x":1}',
        signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
    )
    assert res.paid is True
    assert len(fake_transport.requests) == 2, fake_transport.requests
    for method, _, has_body in fake_transport.requests:
        assert method == "POST"
        assert has_body


@pytest.mark.asyncio
async def test_g7_idempotent_method_keeps_probe(fake_transport):
    """Contrast case: GET keeps the early probe (our probe + the SDK's own
    discovery + paid retry = 3 requests) — only non-idempotent methods skip it."""
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network=NETWORK, max_amount_usd=0.10,
    )
    assert res.paid is True
    assert len(fake_transport.requests) == 3, fake_transport.requests


@pytest.mark.asyncio
async def test_g8_probe_layer_fails_closed_on_omitted_network(fake_transport):
    """G-8 (literal brief case): a configured network + a challenge that OMITS
    the network field must be rejected, not silently accepted."""
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.omit_challenge_network = True

    client = RealX402Client()
    with pytest.raises(ValueError, match="testnet"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="GET", body=None,
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
        )
    # refused before any SDK traffic — only our probe's single request happened.
    assert len(fake_transport.requests) == 1


@pytest.mark.asyncio
async def test_g8_sdk_hook_layer_fails_closed_on_network_mismatch(fake_transport):
    """G-8: for a non-idempotent method (no probe), the SDK-level
    on_before_payment_creation hook must independently refuse a genuinely
    mismatched network — no payload is ever signed."""
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.network = "eip155:8453"  # a real, different network

    client = RealX402Client()
    with pytest.raises(Exception, match="refusing to pay"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
        )
    # aborted before the paid retry: only the SDK's own 402-discovery happened.
    assert len(fake_transport.requests) == 1


# --- Task 4b: the two live-payment blockers, end-to-end against the real SDK.
# Both were flagged in the Task 4 report as "Concerns / adjacent findings" —
# PRE-EXISTING, not caused by G-6..G-12 — and would have blocked/rejected any
# real on-chain payment before this fix:
#   #1 the parser read only the V1 `maxAmountRequired` field, so a
#      standards-compliant V2 challenge (`amount`) never priced;
#   #2 `_networks_match` had no mapping from `WalletConfig.network`
#      ("testnet"/"mainnet", the only two values it can hold) to a real
#      challenge's network id (V1 name or V2 CAIP-2) — the check would reject
#      essentially any real-world payment on network grounds regardless of the
#      G-8 hardening.
# These tests use `legacy_alias=False` (a WIRE-GENUINE V2 challenge, no
# `maxAmountRequired` present at all) and the actual PRODUCTION `network=`
# shape (`"testnet"`/`"mainnet"`, exactly what `service.py` passes from
# `wallet.config.network`) rather than a raw CAIP-2 id, so they fail before
# this fix and pass after it — proving the fix, not just the pure helpers.


@pytest.mark.asyncio
async def test_task4b_quote_prices_genuine_v2_challenge(fake_transport):
    """#1: quote() must price a WIRE-GENUINE V2 challenge (no legacy
    `maxAmountRequired` alias present) — before the fix this returned None
    (fail-closed = quote()/the probe could never price a real V2 server)."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"

    client = RealX402Client()
    price = await client.quote("http://fake/paid")
    assert price == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_task4b_testnet_mode_pays_against_genuine_v2_caip2_challenge(fake_transport):
    """#1 + #2 together, the actual production shape: `network="testnet"`
    (not a raw CAIP-2 id) configured against a WIRE-GENUINE V2 challenge whose
    network is the real CAIP-2 testnet id (`eip155:84532`, the module's
    default `NETWORK`). Must complete a real paid fetch end-to-end.

    Also the "requirement asset = base-sepolia USDC + testnet → allowed"
    asset-pin acceptance case (Task 4b review fix) — `fake_transport.asset`
    defaults to the genuine Base Sepolia USDC contract, matching `network`."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    # fake_transport.network already defaults to NETWORK == "eip155:84532"

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
    )
    assert res.paid is True
    assert res.amount_usd == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_task4b_mainnet_mode_pays_against_genuine_v2_caip2_challenge(fake_transport):
    """#2, the mainnet mirror: `network="mainnet"` configured against a
    challenge whose network is the real CAIP-2 mainnet id (`eip155:8453`).

    Task 4b review fix (CRITICAL): also sets the challenge's asset to the
    genuine Base MAINNET USDC contract (`ASSET_MAINNET`) — the module's
    default `ASSET` is the Base SEPOLIA USDC address, which the asset-pin
    gate now correctly refuses when the configured network is mainnet. This
    is the literal "requirement asset = canonical Base USDC + mainnet →
    allowed" acceptance case."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.network = "eip155:8453"
    fake_transport.asset = ASSET_MAINNET

    client = RealX402Client()
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network="mainnet", max_amount_usd=0.10,
    )
    assert res.paid is True
    assert res.amount_usd == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_task4b_mainnet_mode_rejects_testnet_caip2_prefix_collision(fake_transport):
    """#2, the literal collision case: `network="mainnet"` configured against
    a challenge network of `eip155:84532` (testnet) — must be REJECTED, not
    waved through by a numeric chain-id prefix collision with `eip155:8453`
    (mainnet). Exercises the full paying leg (SDK-hook layer, non-idempotent
    method so no early probe), not just the pure `_networks_match` unit test.

    Also the "requirement asset = base-sepolia USDC + mainnet → rejected
    (network)" acceptance case (Task 4b review fix): `fake_transport.asset`
    stays at its default (genuine Base Sepolia USDC) while `network="mainnet"`
    is configured — rejected for the NETWORK mismatch (checked first), never
    reaching the asset-pin check at all."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    # fake_transport.network already defaults to NETWORK == "eip155:84532" (testnet)

    client = RealX402Client()
    with pytest.raises(Exception, match="refusing to pay"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="mainnet", max_amount_usd=0.10,
        )
    # aborted before the paid retry: only the SDK's own 402-discovery happened.
    assert len(fake_transport.requests) == 1


# --- Task 4b review fix (CRITICAL, money-safety): asset-pin gate -----------
# A malicious x402 resource server naming a requirement in an ERC-20 with
# real on-chain decimals D<6 would, without this gate, get an atomic cap of
# `max_amount_usd * 10**6` regardless of D — at D=0 a $50 cap could authorize
# signing/transferring up to $50,000,000 of that token (see the module
# docstring). These tests exercise the FULL paying leg end-to-end (not just
# the pure `_asset_is_canonical_usdc` unit tests in test_real_client_smoke.py)
# to prove the gate actually blocks real payloads from ever being created.


@pytest.mark.asyncio
async def test_asset_pin_probe_layer_refuses_non_usdc_asset(fake_transport):
    """Probe layer (GET — idempotent, keeps the early probe): a challenge
    naming a non-USDC asset must be refused BEFORE any SDK traffic, even
    though its amount and network are otherwise perfectly compliant."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.asset = NOT_USDC_ASSET

    client = RealX402Client()
    with pytest.raises(ValueError, match="canonical USDC"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="GET", body=None,
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
        )
    # refused by our own probe before any SDK traffic — only 1 request.
    assert len(fake_transport.requests) == 1


@pytest.mark.asyncio
async def test_asset_pin_sdk_hook_layer_refuses_non_usdc_asset_no_signing(fake_transport):
    """SDK-hook layer (POST — non-idempotent, G-7 skips the probe entirely):
    the on_before_payment_creation hook must independently refuse a
    non-USDC asset — this is the SOLE enforcement point for POST/PUT/PATCH/
    DELETE, so if this doesn't hold, an attacker asset-swap on a POST
    resource is undetectable. No payload is ever created/signed: only the
    SDK's own unpaid 402-discovery request happens, never a paid retry."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    fake_transport.asset = NOT_USDC_ASSET

    client = RealX402Client()
    with pytest.raises(Exception, match="refusing to pay"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
        )
    # aborted before the paid retry — no signing happened: only the SDK's own
    # unpaid 402-discovery request was ever issued.
    assert len(fake_transport.requests) == 1


@pytest.mark.asyncio
async def test_asset_pin_d0_spoof_refused_before_decimals_ever_matter(fake_transport):
    """The exact CRITICAL scenario from the review: a non-USDC asset claims
    `decimals=0` with a deceptively TINY atomic amount ("1") — under the OLD
    code (or if the SDK's own decimals-blind `max_amount` policy were the
    only gate), this atomic amount trivially passes any USD cap check
    (1 <= max_amount_usd * 10**6 for any realistic cap), and the amount would
    have been recorded as ~$0.000001 while actually authorizing a transfer of
    1 WHOLE unit of an attacker-chosen, possibly high-value, token (at
    decimals=0, "1 atomic unit" IS "1 whole token" — no shift at all).

    Uses POST (SDK-hook layer only, no probe) so this proves the gate that
    actually stands between the SDK's requirement-selection and payload
    creation — not just the advisory probe. The asset-pin gate refuses this
    UNCONDITIONALLY on the asset mismatch alone: decimals/amount are never
    even consulted by the enforcement path (see _asset_is_canonical_usdc)."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "1"  # would read as $0.000001 if (wrongly) treated as 6-decimal USDC
    fake_transport.settle_amount = "1"
    fake_transport.asset = NOT_USDC_ASSET
    fake_transport.extra = {"name": "Spoofed", "version": "1", "decimals": 0}

    client = RealX402Client()
    with pytest.raises(Exception, match="refusing to pay"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=0.10,
        )
    # refused before any payload was created/signed — no paid retry occurred.
    assert len(fake_transport.requests) == 1


# --- C1 half 2 (2026-07-15): PolicyGate re-check against the SDK-selected -----
# --- requirement's REAL amount, inside on_before_payment_creation, before any -
# --- payload is signed. End-to-end against the real SDK (skipped without it). -
# The actual paying leg is bounded ONLY by the agent-chosen max_amount_usd (the
# SDK's own max_amount policy). These tests prove the WALLET's catastrophic
# ceiling + daily/venue caps now gate the ACTUAL amount too — the C1 fund-drain
# is that a huge max_amount_usd would otherwise let a small-looking quote settle
# far above the wallet's own limits.


@pytest.mark.asyncio
async def test_c1_recheck_aborts_real_amount_over_gate_ceiling(fake_transport):
    """C1 half 2, the fund-drain shape scaled down: the agent authorized
    max_amount_usd=$1.00 (so the SDK's own max_amount policy would happily allow
    the $0.05 challenge), but the wallet PolicyGate's catastrophic ceiling is
    $0.03. The real $0.05 amount the SDK selected must be re-checked against the
    gate inside _abort_if_invalid_requirement and REFUSED before any payload is
    signed. POST (non-idempotent) so the SDK-hook layer is the sole gate — no
    early probe runs at all."""
    from core.wallet.policy import PolicyGate
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"  # $0.05
    fake_transport.settle_amount = "50000"
    gate = PolicyGate(max_per_tx_usd=0.03)

    client = RealX402Client(policy=gate)
    with pytest.raises(Exception, match="wallet policy"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=1.00,
        )
    # aborted before the paid retry: only the SDK's own 402-discovery happened.
    assert len(fake_transport.requests) == 1


@pytest.mark.asyncio
async def test_c1_recheck_aborts_when_daily_cap_would_exceed(fake_transport):
    """C1 half 2, daily-cap leg: a generous per-tx ceiling ($10) but a $0.10
    daily cap with $0.08 already spent — the real $0.05 amount pushes
    trailing-24h to $0.13 > $0.10, so the re-check REFUSES even though
    max_amount_usd=$1.00 and the per-tx ceiling would allow it. GET keeps the
    early probe, but the probe can't see the daily-cap state; this proves the
    SDK-hook re-check independently consults the wallet's rolling window."""
    from core.wallet.policy import PolicyGate
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    gate = PolicyGate(max_per_tx_usd=10.0, daily_cap_usd=0.10)
    gate.record(venue="x402", action="pay", amount_usd=0.08, counterparty="0xA",
                idempotency_key="k1", result_ref="t1")

    client = RealX402Client(policy=gate)
    with pytest.raises(Exception, match="wallet policy"):
        await client.fetch_with_payment(
            url="http://fake/paid", method="POST", body="{}",
            signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=1.00,
        )
    assert len(fake_transport.requests) == 1


@pytest.mark.asyncio
async def test_c1_recheck_allows_within_gate_and_pays(fake_transport):
    """Happy path: a generous gate ($10 ceiling, no daily cap) re-checks the
    real $0.05 amount, allows it, and the fetch pays end-to-end."""
    from core.wallet.policy import PolicyGate
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"
    gate = PolicyGate(max_per_tx_usd=10.0)

    client = RealX402Client(policy=gate)
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=1.00,
    )
    assert res.paid is True
    assert res.amount_usd == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_c1_no_policy_threaded_is_backward_compatible(fake_transport):
    """Backward-compat: RealX402Client() with NO policy threaded in keeps the
    pre-C1-half-2 behavior (the re-check layer is inert). Production always
    threads the policy via service._get_client; a direct client with no policy
    still pays, so existing SDK tests that construct RealX402Client() are
    unaffected."""
    fake_transport.legacy_alias = False
    fake_transport.challenge_amount = "50000"
    fake_transport.settle_amount = "50000"

    client = RealX402Client()  # no policy
    res = await client.fetch_with_payment(
        url="http://fake/paid", method="GET", body=None,
        signer=LocalEoaSigner(KEY), network="testnet", max_amount_usd=1.00,
    )
    assert res.paid is True
