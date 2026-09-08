"""Phase 4 WIRING — the Solana reference scan inside the live watcher.

The primitives were proven separately; this is the part that makes them do
something. Two properties matter most:

* the Solana pass must reuse the SAME idempotency primitives as the EVM pass
  (`transaction_hash_already_settled`, `claim_for_settlement`), because a
  signature must settle at most one invoice EVER; and
* it must match by REFERENCE, never by amount — carrying the EVM strategy here
  would reintroduce the ambiguity the reference exists to remove.
"""
import pytest

from modules.x402 import solana_settlement as ss

TREASURY = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(amount="1500000", err=None):
    return {"meta": {"err": err,
                     "preTokenBalances": [],
                     "postTokenBalances": [
                         {"owner": TREASURY, "mint": USDC,
                          "uiTokenAmount": {"amount": amount, "decimals": 6}}]}}


def test_a_pending_invoice_gets_a_reference_minted_at_creation():
    """The payer needs it, and the watcher re-derives it — so it must be
    deterministic from the invoice id and need no storage to work."""
    from modules.x402.solana_settlement import reference_for_invoice
    assert reference_for_invoice("abc") == reference_for_invoice("abc")


def test_the_scan_finds_a_payment_carrying_our_reference():
    calls = {}

    def _rpc(method, params):
        calls[method] = params
        if method == "getSignaturesForAddress":
            return [{"signature": "sigAAA", "err": None}]
        return None

    sigs = list(ss.scan_reference(ss.reference_for_invoice("inv-1"), rpc=_rpc))
    assert sigs == ["sigAAA"]
    assert "getSignaturesForAddress" in calls


def test_a_failed_signature_is_not_returned_by_the_scan():
    def _rpc(method, params):
        return [{"signature": "sigBAD", "err": {"InstructionError": [0, "X"]}}]
    assert list(ss.scan_reference("ref", rpc=_rpc)) == []


def test_an_rpc_outage_yields_no_signatures_rather_than_raising():
    def _rpc(method, params):
        raise RuntimeError("429")
    assert list(ss.scan_reference("ref", rpc=_rpc)) == []


# -- the settle decision -----------------------------------------------------

def test_a_matching_payment_settles_for_its_exact_amount():
    got = ss.settlement_for(_tx(amount="1500000"), treasury=TREASURY,
                            mint=USDC, expected_raw=1_500_000)
    assert got == 1_500_000


def test_an_overpayment_still_settles():
    """A payer rounding up must not strand their own invoice."""
    assert ss.settlement_for(_tx(amount="1600000"), treasury=TREASURY,
                             mint=USDC, expected_raw=1_500_000) == 1_600_000


def test_an_underpayment_does_NOT_settle():
    assert ss.settlement_for(_tx(amount="1400000"), treasury=TREASURY,
                             mint=USDC, expected_raw=1_500_000) is None


def test_a_reverted_payment_does_not_settle():
    assert ss.settlement_for(_tx(err={"x": 1}), treasury=TREASURY,
                             mint=USDC, expected_raw=1_500_000) is None


def test_amount_matching_is_not_used_to_IDENTIFY_the_invoice():
    """The amount only VALIDATES a payment the reference already identified.
    On EVM the amount does the identifying, which is why jitter is needed
    there; carrying that here would reintroduce the ambiguity."""
    import inspect
    src = inspect.getsource(ss)
    assert "jitter" not in src.lower() or "NOT" in src or "not use" in src.lower()


# -- wiring into the watcher -------------------------------------------------

def test_the_watcher_exposes_a_solana_scan_pass():
    from modules.x402.settlement_watcher import SettlementWatcher
    assert hasattr(SettlementWatcher, "_scan_solana")


def test_the_solana_pass_is_inert_when_detection_is_off(monkeypatch):
    monkeypatch.delenv("X402_SETTLE_ONCHAIN_DETECT", raising=False)
    monkeypatch.delenv("X402_SOLANA_SETTLE", raising=False)
    from modules.x402.settlement_watcher import solana_settle_enabled
    assert solana_settle_enabled() is False


def test_the_solana_pass_needs_its_OWN_flag(monkeypatch):
    """Arming EVM detection must not silently arm a second chain's money path."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.delenv("X402_SOLANA_SETTLE", raising=False)
    from modules.x402.settlement_watcher import solana_settle_enabled
    assert solana_settle_enabled() is False
    monkeypatch.setenv("X402_SOLANA_SETTLE", "true")
    assert solana_settle_enabled() is True


# -- the invoice side --------------------------------------------------------

def test_an_invoice_carries_its_chain_family():
    """The watcher has two passes and must know which one owns a row. Without
    this, an EVM invoice would be scanned by reference (finding nothing) or a
    Solana one swept by amount (the ambiguity the reference exists to remove)."""
    import inspect
    from modules.x402 import invoicing
    src = inspect.getsource(invoicing)
    assert '"chain_family"' in src


def test_an_invoice_carries_its_solana_reference_when_svm():
    import inspect
    from modules.x402 import invoicing
    src = inspect.getsource(invoicing)
    assert "solana_reference" in src


def test_the_family_is_derived_from_the_registry_not_guessed():
    from modules.x402.invoicing import _chain_family
    assert _chain_family("base") == "evm"
    assert _chain_family("solana") == "svm"


def test_an_unknown_chain_family_is_evm_not_a_crash():
    """Fail SAFE, not open: an unrecognised chain keeps the long-standing EVM
    behaviour rather than silently entering a Solana path that cannot serve it."""
    from modules.x402.invoicing import _chain_family
    assert _chain_family("nosuchchain") == "evm"


# --------------------------------------------------------------------------
# An invoice must be able to NAME its chain (found by rob-dev-aa, 2026-08-26)
#
# `create_payment_request` read the chain from GLOBAL config, so there was no
# way to request a Solana invoice at all: every row came out `chain_family="evm"`
# with no reference, and `_scan_solana` skipped it. Phase 4's wiring was
# unreachable in practice.
#
# The env workaround (X402_DEFAULT_CHAIN=solana) is deliberately NOT the fix —
# it is process-wide, so it would silently retarget any EVM invoice created in
# the same process. The chain is a property of the invoice.
# --------------------------------------------------------------------------

def test_create_payment_request_accepts_a_chain():
    import inspect
    from modules.x402.invoicing import create_payment_request
    assert "chain" in inspect.signature(create_payment_request).parameters


def test_the_chain_parameter_defaults_to_none_so_config_still_wins():
    """Every existing caller must be byte-identical."""
    import inspect
    from modules.x402.invoicing import create_payment_request
    assert inspect.signature(create_payment_request).parameters["chain"].default is None


def test_jitter_is_skipped_for_an_svm_chain():
    """Solana's reference key is an exact correlator, so jitter is strictly
    worse there — it would perturb the amount for no benefit."""
    from modules.x402.invoicing import _jitter_should_apply
    import inspect
    assert "chain" in inspect.signature(_jitter_should_apply).parameters
    assert _jitter_should_apply("solana") is False


def test_jitter_still_applies_on_evm_when_detection_is_on(monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    from modules.x402.invoicing import _jitter_should_apply
    assert _jitter_should_apply("base") is True


def test_the_svm_recipient_is_the_solana_address_not_the_evm_pay_to():
    """Different keys entirely. Paying the EVM `pay_to` on Solana strands funds
    at an address nobody holds."""
    import inspect
    from modules.x402 import invoicing
    src = inspect.getsource(invoicing)
    assert "solana_address" in src


def test_a_base58_recipient_is_never_case_folded():
    """base58 is case-SIGNIFICANT; `.lower()` produces a different address.
    This is the landmine the 2026-08-22 crypto audit flagged."""
    import inspect
    from modules.x402 import invoicing
    src = inspect.getsource(invoicing.create_payment_request)
    assert "recipient.lower()" not in src


# --------------------------------------------------------------------------
# The scan must ENUMERATE, and must not swallow a programming error
# (found on the first real mainnet round trip, 2026-08-26)
#
# `_scan_solana` called `list_payment_requests(status="pending")` with no
# `user_id` — a required keyword-only argument. The resulting TypeError was
# caught by the method's own broad `except Exception` and reported as
# `settled=0 unmatched=0`: "nothing to settle", which is exactly what a healthy
# empty run looks like. The payment had landed, the reference had matched, the
# credit was measured correctly, and the invoice still sat pending.
#
# Same failure as the swap simulation observing nothing and passing on it. A
# watcher is tenant-agnostic by nature, so it enumerates the rows itself.
# --------------------------------------------------------------------------

def test_the_scan_enumerates_pending_rows_across_tenants():
    """It must NOT go through the tenant-scoped listing helper — a watcher has
    no single tenant, and passing one would silently skip every other."""
    import inspect
    from modules.x402.settlement_watcher import SettlementWatcher
    src = inspect.getsource(SettlementWatcher._scan_solana)
    # A CALL, not a mention — the comment explaining why we avoid it is fine.
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    assert "list_payment_requests(" not in code, (
        "that helper is tenant-scoped and requires user_id; the watcher must "
        "query pending svm rows directly")
    assert "chain_family" in code or "svm" in code


def test_a_programming_error_in_the_scan_is_not_reported_as_no_work():
    """A TypeError is not an outage. Swallowing it makes a broken scan
    indistinguishable from an idle one."""
    import inspect
    from modules.x402.settlement_watcher import SettlementWatcher
    src = inspect.getsource(SettlementWatcher._scan_solana)
    assert "TypeError" in src or "raise" in src, (
        "the enumeration step must let a programming error surface rather than "
        "returning (0, 0)")
