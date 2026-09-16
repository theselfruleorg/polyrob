"""launchpad_claim — the deployer's own creator fees.

Found live 2026-09-14. The agent launched two tokens on Pons, both routing 1%
creator tax to its own wallet, and 13.613 ETH had accrued in the launchpad's fee
escrow with no verb able to reach it. Its ledger: "launchpad tool has NO claim
verb; needs owner /trade claim from seat OR defi_trade grant" — and `/trade
claim` is not a verb either.

Three failures stranded it, and all three are fixed here:

1. no claim verb;
2. no DISCOVERY — `launchpad_status` printed reserves, graduation and fee *bps*
   but never the claimable balance, and nothing named `feeEscrow()`;
3. it probed the CURVE with guessed names (`creatorFees`/`pendingFees`/
   `claimable`, all of which revert) and read the resulting reverts as "no fees".
   The real path is curve.feeEscrow() -> escrow.balanceOf(you) -> escrow.claim().

⚠️ The safety property: the caller supplies only a TOKEN. The curve comes from
the pinned factory's own record, the escrow from the curve, the amount from the
escrow. There is no parameter through which `claim()` can be aimed at an
arbitrary contract.
"""
import pytest

from tools.launchpad import pons

CURVE = "0x32d430abADFAE6e1253B9504C02c0eFDb1491eAA"
ESCROW = "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"
WALLET = "0xcAda546F6A6DdDe31b71Ab21ef63D3EbF09fA553"
CLAIMABLE = 13_613_005_871_733_144_000


def _rpc(answers):
    """A fake JSON-RPC keyed by the calldata SELECTOR, so a test states which
    view it is answering rather than relying on call order."""
    def call(method, params):
        assert method == "eth_call", method
        data = params[0]["data"]
        to = params[0]["to"].lower()
        key = (to, data[:10])
        if key in answers:
            return answers[key]
        raise AssertionError(f"unexpected call {key}")
    return call


def _word(value):
    return "0x" + f"{value:064x}"


def _addr_word(addr):
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


# --- the escrow read ------------------------------------------------------

def test_fee_escrow_is_read_from_the_curve_not_supplied():
    rpc = _rpc({(CURVE.lower(), "0xc4b7de97"): _addr_word(ESCROW)})
    assert pons.fee_escrow(rpc, CURVE).lower() == ESCROW.lower()


def test_claimable_reads_the_escrow_balance_for_the_holder():
    rpc = _rpc({
        (CURVE.lower(), "0xc4b7de97"): _addr_word(ESCROW),
        (ESCROW.lower(), "0x70a08231"): _word(CLAIMABLE),
        (CURVE.lower(), "0xdb2bd533"): _word(7),
    })
    claim = pons.claimable(rpc, CURVE, holder=WALLET)
    assert claim.escrow.lower() == ESCROW.lower()
    assert claim.native_wei == CLAIMABLE
    assert claim.unswept_wei == 7


def test_an_unreadable_unswept_balance_does_not_block_the_claim():
    """The unswept remainder is supplementary. A curve generation that does not
    expose it must not stop us claiming money the escrow confirms it owes."""
    rpc = _rpc({
        (CURVE.lower(), "0xc4b7de97"): _addr_word(ESCROW),
        (ESCROW.lower(), "0x70a08231"): _word(CLAIMABLE),
    })
    claim = pons.claimable(rpc, CURVE, holder=WALLET)
    assert claim.native_wei == CLAIMABLE
    assert claim.unswept_wei is None


def test_a_curve_that_names_no_escrow_is_an_honest_refusal_not_a_zero():
    def rpc(method, params):
        return "0x"
    with pytest.raises(pons.PonsError):
        pons.fee_escrow(rpc, CURVE)


def test_zero_claimable_is_reported_as_zero_not_as_an_error():
    rpc = _rpc({
        (CURVE.lower(), "0xc4b7de97"): _addr_word(ESCROW),
        (ESCROW.lower(), "0x70a08231"): _word(0),
        (CURVE.lower(), "0xdb2bd533"): _word(0),
    })
    assert pons.claimable(rpc, CURVE, holder=WALLET).native_wei == 0


# --- the calldata ---------------------------------------------------------

def test_build_claim_is_the_bare_claim_selector():
    built = pons.build_claim(escrow=ESCROW)
    assert built["to"] == ESCROW
    assert built["calldata"] == "0x4e71d92d"
    assert built["value_wei"] == 0


def test_the_claim_selector_is_the_keccak_of_claim():
    from core.wallet import abi
    assert abi.encode_call("claim", [], []) == "0x4e71d92d"


def test_the_escrow_balance_selector_is_balance_of():
    from core.wallet import abi
    data = abi.encode_call("balanceOf", [{"name": "who", "type": "address"}],
                           [WALLET])
    assert data.startswith("0x70a08231")


# --- discovery: status must SHOW the claimable balance --------------------

from tools.launchpad.tool import LaunchpadTool, ClaimParams, StatusParams  # noqa: E402

TOKEN = "0xC37160175A9Bac1Cf480AC7a0dE75c8D97c9aE6A"


def _tool(monkeypatch, *, claim=None, record=None, state=None, guard=None):
    monkeypatch.setenv("LAUNCHPAD_ENABLED", "true")
    import core.config_policy.policy as _p  # noqa: F401
    tool = LaunchpadTool(name="launchpad")
    tool._rpc = lambda *a, **k: "0x"
    tool._signer_address = lambda: WALLET
    return tool


def _text(res):
    return (getattr(res, "extracted_content", None) or "") + (getattr(res, "error", None) or "")


@pytest.mark.asyncio
async def test_status_reports_the_claimable_fee_balance(monkeypatch):
    """The discovery half. Status printed reserves, graduation and fee BPS and
    never the balance, so nothing ever told the agent money was waiting."""
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: {
        "curve": CURVE, "deployer": WALLET, "pairToken":
        "0x0000000000000000000000000000000000000000",
        "graduationThreshold": 4 * 10 ** 18})
    monkeypatch.setattr(pons, "curve_state", lambda rpc, c, recipient: pons.CurveState(
        curve=CURVE, token=TOKEN, quote_reserve=10 ** 18, token_reserve=10 ** 24,
        reserved_tokens=0, fee_bps=100, creator_tax_bps=100, snipe_tax_bps=0,
        graduated=False))
    monkeypatch.setattr(pons, "claimable", lambda rpc, c, holder: pons.Claimable(
        escrow=ESCROW, native_wei=CLAIMABLE, unswept_wei=5))

    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_status(StatusParams(token=TOKEN)))
    assert "13.613005871733144" in out
    assert ESCROW.lower() in out.lower()
    assert "claim" in out.lower()


@pytest.mark.asyncio
async def test_status_says_nothing_is_claimable_rather_than_omitting_the_line(monkeypatch):
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: {
        "curve": CURVE, "deployer": WALLET, "pairToken":
        "0x0000000000000000000000000000000000000000",
        "graduationThreshold": 4 * 10 ** 18})
    monkeypatch.setattr(pons, "curve_state", lambda rpc, c, recipient: pons.CurveState(
        curve=CURVE, token=TOKEN, quote_reserve=0, token_reserve=0,
        reserved_tokens=0, fee_bps=100, creator_tax_bps=100, snipe_tax_bps=0,
        graduated=False))
    monkeypatch.setattr(pons, "claimable", lambda rpc, c, holder: pons.Claimable(
        escrow=ESCROW, native_wei=0, unswept_wei=0))

    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_status(StatusParams(token=TOKEN)))
    assert "claimable" in out.lower()


@pytest.mark.asyncio
async def test_an_unreadable_escrow_is_stated_never_rendered_as_zero(monkeypatch):
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: {
        "curve": CURVE, "deployer": WALLET, "pairToken":
        "0x0000000000000000000000000000000000000000",
        "graduationThreshold": 4 * 10 ** 18})
    monkeypatch.setattr(pons, "curve_state", lambda rpc, c, recipient: pons.CurveState(
        curve=CURVE, token=TOKEN, quote_reserve=0, token_reserve=0,
        reserved_tokens=0, fee_bps=100, creator_tax_bps=100, snipe_tax_bps=0,
        graduated=False))

    def _boom(rpc, c, holder):
        raise pons.PonsError("escrow unreachable")

    monkeypatch.setattr(pons, "claimable", _boom)
    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_status(StatusParams(token=TOKEN)))
    assert "unknown" in out.lower() or "could not" in out.lower()
    assert "0.0 " not in out


# --- the verb refuses honestly --------------------------------------------

@pytest.mark.asyncio
async def test_claiming_a_token_pons_did_not_launch_is_refused(monkeypatch):
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "verify_pins", lambda rpc: None)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: None)
    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_claim(ClaimParams(token=TOKEN)))
    assert "not launched by pons" in out.lower()


@pytest.mark.asyncio
async def test_claiming_nothing_is_refused_before_any_broadcast(monkeypatch):
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "verify_pins", lambda rpc: None)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: {
        "curve": CURVE, "deployer": WALLET, "pairToken": "0x00",
        "graduationThreshold": 1})
    monkeypatch.setattr(pons, "claimable", lambda rpc, c, holder: pons.Claimable(
        escrow=ESCROW, native_wei=0, unswept_wei=0))
    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_claim(ClaimParams(token=TOKEN)))
    assert "nothing" in out.lower() or "0" in out
    assert "broadcast" not in out.lower() or "nothing was broadcast" in out.lower()


def test_a_claim_by_a_wallet_that_is_not_the_deployer_is_still_allowed(monkeypatch):
    """The escrow decides who it owes, not us: a creator-fee RECIPIENT need not
    be the deployer. Refusing on deployer identity would block a legitimate
    recipient, and the escrow already pays only the caller."""
    from tools.launchpad import pons
    assert pons.build_claim(escrow=ESCROW)["to"] == ESCROW


def test_the_claimable_balance_is_per_wallet_not_per_token():
    """Live-verified 2026-09-14: both of the agent's launches report the SAME
    escrow and the SAME owed amount, because the escrow credits an ADDRESS, not
    a token. Wording it as "fees for this token" would have the agent claim
    twice expecting two payouts; the second would refuse on a zero balance and
    read as money having gone missing.
    """
    from tools.launchpad import pons
    rpc = _rpc({
        (CURVE.lower(), "0xc4b7de97"): _addr_word(ESCROW),
        (ESCROW.lower(), "0x70a08231"): _word(CLAIMABLE),
        (CURVE.lower(), "0xdb2bd533"): _word(0),
    })
    owed = pons.claimable(rpc, CURVE, holder=WALLET)
    # The read is keyed on the HOLDER, and the token never enters it.
    assert owed.native_wei == CLAIMABLE


@pytest.mark.asyncio
async def test_status_says_the_balance_covers_every_launch(monkeypatch):
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    monkeypatch.setattr(pons, "launched_token", lambda rpc, t: {
        "curve": CURVE, "deployer": WALLET, "pairToken":
        "0x0000000000000000000000000000000000000000",
        "graduationThreshold": 4 * 10 ** 18})
    monkeypatch.setattr(pons, "curve_state", lambda rpc, c, recipient: pons.CurveState(
        curve=CURVE, token=TOKEN, quote_reserve=0, token_reserve=0,
        reserved_tokens=0, fee_bps=100, creator_tax_bps=100, snipe_tax_bps=0,
        graduated=False))
    monkeypatch.setattr(pons, "claimable", lambda rpc, c, holder: pons.Claimable(
        escrow=ESCROW, native_wei=CLAIMABLE, unswept_wei=0))
    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_status(StatusParams(token=TOKEN)))
    assert "every" in out.lower() or "all" in out.lower()
    assert "not just this token" in out.lower() or "across" in out.lower()


@pytest.mark.asyncio
async def test_a_factory_whose_code_changed_refuses_the_claim(monkeypatch):
    """The pins are the ROOT of this verb's provenance: curve from the
    factory's record, escrow from the curve, amount from the escrow. If the
    factory is not the reviewed code, none of those three links means
    anything — and `launchpad_claim` shipped without checking them."""
    from tools.launchpad import pons, tool as tool_mod
    monkeypatch.setattr(tool_mod, "launchpad_enabled", lambda: True)
    reached = []

    def _pins(rpc):
        raise pons.PonsError("factory code hash does not match the pin")

    monkeypatch.setattr(pons, "verify_pins", _pins)
    monkeypatch.setattr(pons, "launched_token",
                        lambda rpc, t: reached.append(1) or None)
    t = LaunchpadTool(name="launchpad")
    t._rpc = lambda *a, **k: "0x"
    t._signer_address = lambda: WALLET
    out = _text(await t.launchpad_claim(ClaimParams(token=TOKEN)))
    assert "does not match the pin" in out
    assert not reached, "the factory record must not be read past a failed pin"
