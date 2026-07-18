import importlib.util
import pytest

x402_installed = importlib.util.find_spec("x402") is not None


def test_real_client_class_importable():
    from tools.x402.real_client import RealX402Client
    assert RealX402Client is not None


@pytest.mark.skipif(not x402_installed, reason="x402 SDK not installed in this env")
def test_real_client_constructs():
    from tools.x402.real_client import RealX402Client
    RealX402Client()  # must not raise when the SDK is present


# --- pure-helper regressions (no SDK needed) -------------------------------

import base64 as _b64
import json as _json


class _Resp:
    def __init__(self, status_code=402, challenge=None):
        self.status_code = status_code
        self.headers = {}
        if challenge is not None:
            enc = _b64.b64encode(_json.dumps(challenge).encode()).decode()
            self.headers["PAYMENT-REQUIRED"] = enc


def test_parse_402_challenge_extracts_amount_network_payto():
    from tools.x402.real_client import RealX402Client as C
    ch = {"accepts": [{"maxAmountRequired": "100000", "network": "base-sepolia",
                       "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.1)  # 100000 / 1e6 USDC
    assert out["network"] == "base-sepolia"
    assert out["pay_to"] == "0xabc"


def test_parse_402_challenge_absent_header_is_none():
    from tools.x402.real_client import RealX402Client as C
    assert C._parse_402_challenge(_Resp(200)) is None


def test_parse_402_challenge_unparseable_marked_not_free():
    from tools.x402.real_client import RealX402Client as C
    r = _Resp(402)
    r.headers["PAYMENT-REQUIRED"] = "!!!not-base64!!!"
    out = C._parse_402_challenge(r)
    assert out is not None and out["amount"] is None and out.get("_unparseable")


# --- Task 4b: V2 challenge field name (`amount` vs V1 `maxAmountRequired`) --


def test_parse_402_challenge_v1_named_challenge_priced_via_x402version_1():
    """V1 challenge, EXPLICIT x402Version=1: priced from `maxAmountRequired`."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 1, "accepts": [{"maxAmountRequired": "100000",
                                         "network": "base-sepolia", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.1)


def test_parse_402_challenge_v2_amount_field_priced_correctly():
    """V2 challenge (x402Version=2) carries the amount as `amount`, NOT
    `maxAmountRequired` — this is the PRE-EXISTING bug: the parser used to read
    only the V1 field name, so a real V2 challenge always priced as None
    (fail-closed = quote()/the probe could never price a real V2 server)."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 2, "accepts": [{"amount": "50000",
                                         "network": "eip155:84532", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.05)  # 50000 / 1e6 USDC
    assert out["network"] == "eip155:84532"
    assert out["pay_to"] == "0xabc"


def test_parse_402_challenge_no_version_marker_tries_both_field_names():
    """No `x402Version` at all (the pre-existing test fixture shape, and some
    non-compliant servers): still priced via the V1-name fallback — unchanged
    legacy behavior, now ALSO falls back to `amount` if `maxAmountRequired` is
    absent (covered by the next test)."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"accepts": [{"maxAmountRequired": "100000", "network": "base-sepolia",
                       "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.1)


def test_parse_402_challenge_no_version_marker_falls_back_to_v2_field():
    from tools.x402.real_client import RealX402Client as C
    ch = {"accepts": [{"amount": "20000", "network": "eip155:84532", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.02)


def test_parse_402_challenge_unknown_version_neither_field_fails_closed():
    """An unrecognised x402Version (neither 1 nor 2) whose entry carries
    neither `maxAmountRequired` nor `amount` must stay unpriced (None) — the
    existing fail-closed behavior downstream (quote()/the probe both refuse to
    auto-pay an unreadable amount) must be unchanged, not crash or guess."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 3, "accepts": [{"network": "base-sepolia", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out is not None
    assert out["amount"] is None
    assert out["network"] == "base-sepolia"


def test_parse_402_challenge_respects_asset_decimals_when_present():
    """Task 4b: atomic→USD conversion must respect the challenge's own asset
    decimals when the challenge carries them (nested under `extra`, where the
    SDK itself places EIP-712 domain metadata), not hardcode USDC's 6."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 2, "accepts": [{
        "amount": "1500000000000000000",  # 1.5 * 10**18
        "network": "eip155:8453", "payTo": "0xabc",
        "extra": {"name": "MegaUSD", "version": "1", "decimals": 18},
    }]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(1.5)


def test_parse_402_challenge_decimals_default_6_when_absent():
    """No decimals anywhere on the challenge → default stays 6 (USDC), the
    existing/unchanged behavior for every other test in this file."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 2, "accepts": [{"amount": "1000000",
                                         "network": "eip155:8453", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(1.0)


# --- Task 4b: testnet/mainnet → V1 name / V2 CAIP-2 network mapping --------


def test_networks_match_mainnet_allows_v1_name_and_caip2():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("mainnet", "base") is True
    assert C._networks_match("mainnet", "eip155:8453") is True


def test_networks_match_mainnet_rejects_testnet_v1_name():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("mainnet", "base-sepolia") is False


def test_networks_match_mainnet_rejects_testnet_caip2_prefix_collision():
    """The literal collision case: eip155:8453 (Base mainnet) vs eip155:84532
    (Base Sepolia testnet) — a numeric chain-id PREFIX collision the old
    substring/prefix `_networks_match` would have let through."""
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("mainnet", "eip155:84532") is False


def test_networks_match_testnet_allows_v1_name_and_caip2():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("testnet", "base-sepolia") is True
    assert C._networks_match("testnet", "eip155:84532") is True


def test_networks_match_testnet_rejects_mainnet_v1_name():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("testnet", "base") is False


def test_networks_match_testnet_rejects_mainnet_caip2_prefix_collision():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("testnet", "eip155:8453") is False


def test_networks_match_mode_matching_is_case_insensitive():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("MAINNET", "Base") is True
    assert C._networks_match("Testnet", "EIP155:84532") is True


def test_networks_match_exact_fallback_when_configured_not_a_mode():
    """When `configured` isn't a recognised mode ("testnet"/"mainnet") — e.g. a
    raw network id passed directly, as some callers/tests do — matching falls
    back to exact string equality, NEVER substring/prefix (that was the old
    bug: "base" used to substring-match "eip155:base-sepolia")."""
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("base-sepolia", "base-sepolia") is True
    assert C._networks_match("base", "eip155:base-sepolia") is False  # no more substring match
    assert C._networks_match("base-mainnet", "base-sepolia") is False
    assert C._networks_match(None, "base-sepolia") is True  # nothing configured → don't block
    assert C._networks_match("", "base-sepolia") is True  # falsy configured → don't block


def test_networks_match_fail_closed_on_omitted_challenge_network():
    # G-8: a configured network + a challenge that omits/blanks network must FAIL
    # CLOSED, not silently pass through (the old bug: `not challenged` returned True).
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("mainnet", None) is False
    assert C._networks_match("mainnet", "") is False


# --- Task 4b review fix (CRITICAL, money-safety): asset-pin gate -----------
# _asset_is_canonical_usdc is the actual enforcement gate that makes the
# hardcoded 6-decimal _USDC_ATOMIC_PER_USD math correct by construction — see
# the module docstring. These are the addresses the SDK itself ships as its
# own default USDC deployments (verified against x402==2.15.0's
# x402.mechanisms.evm.constants / .v1.constants), matching core.wallet.onchain.

_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
_NOT_USDC = "0x000000000000000000000000000000deadbeef"  # some other ERC-20, on neither network


def test_asset_is_canonical_usdc_mainnet_matches_canonical_address():
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("mainnet", _MAINNET_USDC) is True
    # address comparison is case-insensitive (checksum vs lowercase)
    assert C._asset_is_canonical_usdc("mainnet", _MAINNET_USDC.upper().replace("0X", "0x")) is True


def test_asset_is_canonical_usdc_testnet_matches_canonical_address():
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("testnet", _SEPOLIA_USDC) is True


def test_asset_is_canonical_usdc_rejects_non_usdc_asset():
    """The core exploit this gate closes: a resource server naming a
    requirement in some OTHER ERC-20 (which may carry any real on-chain
    decimals, including < 6) must be refused regardless of network."""
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("mainnet", _NOT_USDC) is False
    assert C._asset_is_canonical_usdc("testnet", _NOT_USDC) is False


def test_asset_is_canonical_usdc_rejects_wrong_network_usdc():
    """Asset-pin is network-scoped, not just 'is this USDC anywhere' — the
    mainnet USDC contract is NOT canonical when configured for testnet, and
    vice versa (they are genuinely different deployments/decimals-bearing
    contracts, even though both happen to be 6-decimal USDC today)."""
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("testnet", _MAINNET_USDC) is False
    assert C._asset_is_canonical_usdc("mainnet", _SEPOLIA_USDC) is False


def test_asset_is_canonical_usdc_accepts_v1_name_and_caip2_alias():
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("base", _MAINNET_USDC) is True
    assert C._asset_is_canonical_usdc("eip155:8453", _MAINNET_USDC) is True
    assert C._asset_is_canonical_usdc("base-sepolia", _SEPOLIA_USDC) is True
    assert C._asset_is_canonical_usdc("eip155:84532", _SEPOLIA_USDC) is True


def test_asset_is_canonical_usdc_fails_closed_on_missing_inputs():
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc(None, _MAINNET_USDC) is False
    assert C._asset_is_canonical_usdc("mainnet", None) is False
    assert C._asset_is_canonical_usdc("", _MAINNET_USDC) is False
    assert C._asset_is_canonical_usdc("mainnet", "") is False


def test_asset_is_canonical_usdc_fails_closed_on_unrecognised_network():
    """A `configured` value with no canonical asset on record must fail
    closed (refuse) — never fall back to a weaker comparison."""
    from tools.x402.real_client import RealX402Client as C
    assert C._asset_is_canonical_usdc("some-other-chain", _MAINNET_USDC) is False


def test_parse_402_challenge_extracts_asset_field():
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 2, "accepts": [{"amount": "50000", "network": "eip155:8453",
                                         "payTo": "0xabc", "asset": _MAINNET_USDC}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["asset"] == _MAINNET_USDC


def test_parse_402_challenge_asset_absent_is_none():
    from tools.x402.real_client import RealX402Client as C
    ch = {"accepts": [{"maxAmountRequired": "100000", "network": "base-sepolia", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["asset"] is None


def test_parse_402_challenge_d0_spoof_amount_is_readable_but_asset_is_not_usdc():
    """The D=0 spoof scenario at the parsing layer: a non-USDC asset claiming
    decimals=0 with a deceptively tiny atomic amount still parses fine (this
    parser doesn't know about asset-pinning — that gate lives in
    fetch_with_payment's probe/SDK-hook layers, see
    test_real_client_sdk_integration.py). This test only pins down that the
    parsed `asset` is exactly what lets the downstream gate refuse it,
    independent of how the amount/decimals happen to read."""
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": 2, "accepts": [{
        "amount": "1", "network": "eip155:8453", "payTo": "0xabc",
        "asset": _NOT_USDC, "extra": {"name": "Spoofed", "version": "1", "decimals": 0},
    }]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["asset"] == _NOT_USDC
    assert out["amount"] == pytest.approx(1.0)  # 1 / 10**0 — decimals=0 taken at face value
    from tools.x402.real_client import RealX402Client as C2
    assert C2._asset_is_canonical_usdc("mainnet", out["asset"]) is False


# --- Minor (Task 4 review): tolerate a stringy x402Version marker ----------


def test_parse_402_challenge_v2_tolerates_string_version_marker():
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": "2", "accepts": [{"amount": "75000", "network": "eip155:8453",
                                           "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.075)


def test_parse_402_challenge_v1_tolerates_string_version_marker():
    from tools.x402.real_client import RealX402Client as C
    ch = {"x402Version": "1", "accepts": [{"maxAmountRequired": "80000",
                                           "network": "base-sepolia", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.08)


# --- G-9: facilitator_url is no longer a client-side parameter --------------

def test_fetch_with_payment_signature_has_no_facilitator_url():
    """G-9: the x402 CLIENT never chooses/contacts a facilitator (verify/settle
    is the resource SERVER's concern — see the module docstring's SDK-inspection
    notes). fetch_with_payment must not accept a facilitator_url argument at all
    — not even a silently-ignored one. This only inspects the function's
    signature (the SDK imports inside the method body are lazy), so it needs no
    SDK install."""
    import inspect
    from tools.x402.real_client import RealX402Client
    sig = inspect.signature(RealX402Client.fetch_with_payment)
    assert "facilitator_url" not in sig.parameters


def test_x402_payment_client_protocol_has_no_facilitator_url():
    """The shared Protocol + FakeX402Client test double must match — a divergent
    interface would defeat the point of X402PaymentClient as one contract."""
    import inspect
    from tools.x402.client import X402PaymentClient, FakeX402Client
    assert "facilitator_url" not in inspect.signature(
        X402PaymentClient.fetch_with_payment).parameters
    assert "facilitator_url" not in inspect.signature(
        FakeX402Client.fetch_with_payment).parameters


# --- Task 4 review Finding 1 (G-6 scope): settle success=False must NOT be --
# --- treated as a confirmed payment. Pure decision logic, no SDK needed. ---

def test_reconcile_settle_success_false_flips_to_unpaid_and_logs_loudly(caplog):
    """A resource server can answer HTTP 200 with a decoded settle response
    whose success=False (on-chain settlement FAILED). That must NOT be treated
    like a successful payment: paid flips to False, the amount is zeroed (never
    recorded as a confirmed spend), and a loud logger.error names the tx/amount."""
    from tools.x402.real_client import RealX402Client as C

    settle = {"amount": 0.05, "network": "testnet", "payer": "0xPAYER",
              "tx_hash": "0xDEADBEEF", "success": False}
    payment_info = {"happened": True, "amount": 0.05, "pay_to": "0xR"}

    with caplog.at_level("ERROR"):
        paid, amount_paid, amount_is_estimate = C._reconcile_paid_amount(
            paid=True, settle=settle, payment_info=payment_info,
            probe_amount=0.05, max_amount_usd=0.10, url="http://fake/paid",
        )

    assert paid is False
    assert amount_paid == 0.0
    assert amount_is_estimate is False
    assert any(
        r.levelname == "ERROR" and "settlement FAILED" in r.message
        and "0xDEADBEEF" in r.message
        for r in caplog.records
    )


def test_reconcile_settle_absent_keeps_paid_true_marks_estimate():
    """No settle header at all (older/absent header): keep the existing
    behavior of falling back to the exact signed amount, but mark it an
    estimate — it is not the authoritative settled figure."""
    from tools.x402.real_client import RealX402Client as C

    payment_info = {"happened": True, "amount": 0.03, "pay_to": "0xR"}
    paid, amount_paid, amount_is_estimate = C._reconcile_paid_amount(
        paid=True, settle=None, payment_info=payment_info,
        probe_amount=0.03, max_amount_usd=0.10, url="http://fake/paid",
    )
    assert paid is True
    assert amount_paid == pytest.approx(0.03)
    assert amount_is_estimate is True


def test_reconcile_settle_success_true_is_authoritative_not_estimate():
    """settle.success explicitly True: the decoded settled amount is
    authoritative, not an estimate."""
    from tools.x402.real_client import RealX402Client as C

    settle = {"amount": 0.06, "network": "testnet", "payer": "0xPAYER",
              "tx_hash": "0xTXHASH", "success": True}
    payment_info = {"happened": True, "amount": 0.04, "pay_to": "0xR"}
    paid, amount_paid, amount_is_estimate = C._reconcile_paid_amount(
        paid=True, settle=settle, payment_info=payment_info,
        probe_amount=0.04, max_amount_usd=0.10, url="http://fake/paid",
    )
    assert paid is True
    assert amount_paid == pytest.approx(0.06)
    assert amount_is_estimate is False


def test_reconcile_settle_success_unknown_marks_estimate():
    """settle header present with a readable amount but no `success` field
    (unknown/None, not explicitly True or False): we can't confirm settlement,
    so mark the amount an estimate rather than authoritative — but do NOT
    treat it as a failure (that requires success is explicitly False)."""
    from tools.x402.real_client import RealX402Client as C

    settle = {"amount": 0.05, "network": "testnet", "payer": "0xPAYER",
              "tx_hash": "0xTXHASH", "success": None}
    payment_info = {"happened": True, "amount": 0.05, "pay_to": "0xR"}
    paid, amount_paid, amount_is_estimate = C._reconcile_paid_amount(
        paid=True, settle=settle, payment_info=payment_info,
        probe_amount=0.05, max_amount_usd=0.10, url="http://fake/paid",
    )
    assert paid is True
    assert amount_paid == pytest.approx(0.05)
    assert amount_is_estimate is True


def test_reconcile_not_paid_passthrough():
    """If the SDK never actually created+signed a payment (paid=False going
    in), reconciliation is a no-op passthrough regardless of any settle data."""
    from tools.x402.real_client import RealX402Client as C

    paid, amount_paid, amount_is_estimate = C._reconcile_paid_amount(
        paid=False, settle={"amount": 99, "success": True}, payment_info={},
        probe_amount=None, max_amount_usd=0.10, url="http://fake/paid",
    )
    assert paid is False
    assert amount_paid == 0.0
    assert amount_is_estimate is False


# --- C1 half 2 (2026-07-15): PolicyGate re-check against the SDK-selected -----
# --- requirement's REAL amount, before any payload is signed. Pure decision --
# --- helper (no SDK needed), mirroring _reconcile_paid_amount/_networks_match.
# The paying leg is bounded only by the agent-chosen max_amount_usd (the SDK's
# own max_amount policy). This re-check puts the WALLET's authoritative gate
# (catastrophic per-tx ceiling + daily/venue caps) directly on the actual
# amount the SDK selected, at the one point before any payload is signed.


def test_policy_recheck_reason_allows_within_caps():
    from tools.x402.real_client import RealX402Client as C
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=10.0)
    assert C._policy_recheck_reason(gate, 0.05) is None


def test_policy_recheck_reason_blocks_real_amount_over_ceiling():
    """The C1 core: the ACTUAL amount the SDK would pay ($0.05) exceeds the
    wallet's catastrophic per-tx ceiling ($0.03), even though the agent
    authorized a much larger max_amount_usd — the re-check must REFUSE."""
    from tools.x402.real_client import RealX402Client as C
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=0.03)
    reason = C._policy_recheck_reason(gate, 0.05)
    assert reason is not None
    assert "0.05" in reason
    assert "wallet policy" in reason.lower()


def test_policy_recheck_reason_blocks_when_daily_cap_exceeded():
    """The daily-cap leg: the real amount is within the per-tx ceiling but the
    trailing-24h spend + the real amount exceeds the daily cap — REFUSE."""
    from tools.x402.real_client import RealX402Client as C
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=0.10)
    gate.record(venue="x402", action="pay", amount_usd=0.08, counterparty="0xA",
                idempotency_key="k1", result_ref="t1")
    reason = C._policy_recheck_reason(gate, 0.05)  # 0.08 + 0.05 = 0.13 > 0.10
    assert reason is not None
    assert "daily" in reason.lower()


def test_policy_recheck_reason_fails_closed_on_unreadable_amount():
    """Money path fails CLOSED: a None real amount (the client couldn't read the
    SDK-selected requirement's amount) must REFUSE, never allow by omission."""
    from tools.x402.real_client import RealX402Client as C
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=10.0)
    reason = C._policy_recheck_reason(gate, None)
    assert reason is not None


def test_policy_recheck_reason_fails_closed_on_raising_gate():
    """A gate that RAISES during the re-check must REFUSE (fail-closed), never
    swallow the error and sign the payment on an unevaluable amount."""
    from tools.x402.real_client import RealX402Client as C

    class _BoomGate:
        def check(self, **kwargs):
            raise RuntimeError("boom-simulated-gate-failure")

    reason = C._policy_recheck_reason(_BoomGate(), 0.05)
    assert reason is not None
    assert "boom" in reason
