"""050 §7.3 — the key signs, the account holds (an NFT's ERC-6551 account).

``TxIntent.via_account`` is not a new shape: every shape but deploy/liquidity may
ride it. The signed transaction is ``{to: account, data: execute(inner…, 0)}``;
the guard judges the INNER call with the account as the holder. 069 v4 (the simple
model): the signing treasury must OWN the NFT — there are no grants and no operator
keys. Every test here pins one of the rules:

1. structural — the signed call IS execute(op 0) on the declared account;
2. account-admin selectors refuse — including a call that does NOT declare
   via_account (otherwise calldata could drive an account while the guard
   measures the treasury);
3. pre-flight reads (pins, footer, lock, owner == signer, state) refuse, fail closed;
and the existing shapes produce IDENTICAL verdicts with and without via_account.
"""
import pytest

from core.wallet import abi, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ACCOUNT = "0xe0f47C083B28C76124129786cBd02a4489b86ec4"
TREASURY = "0x2222222222222222222222222222222222222222"
OWNER = "0x5555555555555555555555555555555555555555"
NFT = "0x4444444444444444444444444444444444444444"
REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
ESCROW = "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"
ZERO = "0x0000000000000000000000000000000000000000"
STATE = 7


class AccountRpc:
    """Pre-flight reads for the token-bound account. Every knob is one refusal."""

    def __init__(self, *, locked=False, state=STATE, impl=erc6551.ACCOUNT_V3_IMPL,
                 pins_ok=True, broken=False, owner=TREASURY, forwarders=(), forwarder_broken=False):
        self.locked, self.state, self.owner = locked, state, owner
        self.impl, self.pins_ok, self.broken = impl, pins_ok, broken
        self.forwarders = {f.lower() for f in forwarders}
        self.forwarder_broken = forwarder_broken
        self.forwarder_reads = []
        self.calls = []

    def __call__(self, method, params):
        if self.broken:
            raise RuntimeError("rpc down")
        if method == "eth_call":
            self.calls.append(params[0]["data"][:10])
        if method == "eth_getCode":
            addr = params[0].lower()
            if addr == ACCOUNT.lower():
                return ("0x363d3d373d3d3d363d73" + self.impl[2:].lower() + "5af43d82803e903d91602b57fd5bf3"
                        + "00" * 128)
            return "0x" + ("00" if self.pins_ok else "01")
        if method == "eth_call":
            sel = params[0]["data"][:10]
            word = lambda n: "0x" + int(n).to_bytes(32, "big").hex()  # noqa: E731
            if sel == abi.selector("isTrustedForwarder(address)"):
                if self.forwarder_broken:
                    raise RuntimeError("isTrustedForwarder reverted")
                who = "0x" + params[0]["data"][-40:]
                self.forwarder_reads.append((params[0]["to"].lower(), who))
                return word(int(who in self.forwarders))
            return {
                abi.selector("isLocked()"): word(int(self.locked)),
                abi.selector("owner()"): word(int(self.owner, 16)),
                abi.selector("state()"): word(self.state),
            }[sel]
        raise AssertionError(method)


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    """The fake chain's registry/impl code is b"\\x00"; pin that (the real pins are tested in test_erc6551)."""
    import hashlib
    h = hashlib.sha256(b"\x00").hexdigest()
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): h,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): h})


def _gate():
    return PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0)


def _outer(inner, *, op=0, to=ACCOUNT, value=0):
    tx = {"to": to, "value": value, "chainId": 4663, "nonce": 5, "gas": 200_000, "maxFeePerGas": 10 ** 8,
          "data": erc6551.encode_execute(inner["to"], inner.get("value", 0), inner.get("data", "0x"), op)}
    return tx


def _set_permissions(callers, allowed):
    """The Tokenbound grant verb — core has no encoder for it (069 v4); tests build it to prove
    the guard refuses it."""
    return abi.encode_call("setPermissions", [{"type": "address[]"}, {"type": "bool[]"}],
                           [list(callers), list(allowed)])


def _run(intent, deltas, tx, *, holder, rpc=None, sim_calls=None):
    def sim(**kw):
        if sim_calls is not None:
            sim_calls.append(kw)
        return deltas
    return tx_guard.authorize(
        intent, tx, holder=holder, gate=_gate(), execution_context=None,
        simulate_fn=sim, price_fn=lambda chain, addr: 1.0,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False,
        account_rpc=rpc or AccountRpc())


def _via(intent, **kw):
    base = dict(intent.__dict__)
    base.update(via_account=ACCOUNT, via_account_state=STATE)
    base.update(kw)
    return tx_guard.TxIntent(**base)


# --- shapes: a transfer ----------------------------------------------------------

def _erc20_intent(**kw):
    base = dict(chain="base", token=USDC, to=TO, amount_raw=250_000, max_spend_usd=5.0,
                idempotency_key="k1")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _erc20_inner():
    return {"to": USDC, "data": "0xa9059cbb" + "00" * 64, "value": 0}


def _erc20_deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={USDC: -250_000}, allowance_deltas={},
                holder_transfers=((USDC.lower(), TO.lower(), 250_000),), gas_used=100_000)
    base.update(kw)
    return Deltas(**base)


def test_an_account_transfer_is_judged_on_the_inner_call_with_the_account_as_holder():
    calls = []
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _outer(_erc20_inner()), holder=TREASURY,
             sim_calls=calls)
    assert d.allowed is True and d.lane == "autonomous", d.reason
    (kw,) = calls
    assert kw["holder"] == ACCOUNT and kw["sender"] == TREASURY
    assert kw["tx"]["to"] == ACCOUNT  # the OUTER transaction is what gets simulated


# --- rule 1: structural -----------------------------------------------------------

@pytest.mark.parametrize("mutate, needle", [
    (lambda tx: tx.update(to=TO), "not the declared token-bound account"),
    (lambda tx: tx.update(value=1), "no value"),
    (lambda tx: tx.update(data=erc6551.encode_execute(USDC, 0, "0x", 1)), "admin"),
    (lambda tx: tx.update(data=erc6551.encode_lock(10 ** 10)), "lock"),
    (lambda tx: tx.update(data=_set_permissions([TO], [True])), "setPermissions"),
    (lambda tx: tx.update(data=erc6551.encode_execute(ACCOUNT, 0, "0x")), "call itself"),
    (lambda tx: tx.update(data=erc6551.encode_execute(USDC, 0, erc6551.encode_lock(1))), "admin"),
    (lambda tx: tx.update(data=erc6551.encode_execute(
        USDC, 0, erc6551.encode_execute(TO, 1, "0x"))), "nested execute"),
    (lambda tx: tx.update(data="0xa9059cbb"), "not execute"),
])
def test_structural_refusals(mutate, needle):
    tx = _outer(_erc20_inner())
    mutate(tx)
    d = _run(_via(_erc20_intent()), _erc20_deltas(), tx, holder=TREASURY)
    assert d.allowed is False and needle in d.reason, d.reason


def test_an_account_call_must_declare_the_state_it_read():
    d = _run(_via(_erc20_intent(), via_account_state=None), _erc20_deltas(), _outer(_erc20_inner()),
             holder=TREASURY)
    assert d.allowed is False and "via_account_state" in d.reason


def test_deploy_and_liquidity_cannot_ride_an_account():
    intent = _via(tx_guard.TxIntent(chain="robinhood", token=None, to=None, amount_raw=0,
                                    max_spend_usd=5.0, is_deploy=True, init_code="0x00"))
    d = _run(intent, _erc20_deltas(), _outer(_erc20_inner()), holder=TREASURY)
    assert d.allowed is False and "deploy" in d.reason


@pytest.mark.parametrize("data", [
    erc6551.encode_execute(USDC, 0, "0xa9059cbb"),
    _set_permissions([TO], [True]),
    abi.selector("executeBatch((address,uint256,bytes,uint8)[])") + "00" * 32,
])
def test_an_undeclared_account_call_refuses(data):
    """⚠️ The bypass: without this, calldata carrying execute() aimed at an account
    would be simulated with the TREASURY as holder — whose balances do not
    move — and every account rule would be skipped."""
    tx = {"to": ACCOUNT, "data": data, "value": 0, "chainId": 4663}
    d = _run(_erc20_intent(), _erc20_deltas(), tx, holder=TREASURY)
    assert d.allowed is False and "via_account" in d.reason


# --- rule 3: pre-flight ------------------------------------------------------------

@pytest.mark.parametrize("rpc, needle", [
    (AccountRpc(locked=True), "LOCKED"),
    (AccountRpc(owner=OWNER), "does not own"),
    (AccountRpc(state=STATE + 1), "state moved"),
    (AccountRpc(impl="0x" + "99" * 20), "not the pinned AccountV3"),
    (AccountRpc(pins_ok=False), "un-reviewed"),
    (AccountRpc(broken=True), "failing closed"),
])
def test_preflight_refusals(rpc, needle):
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _outer(_erc20_inner()), holder=TREASURY, rpc=rpc)
    assert d.allowed is False and needle in d.reason, d.reason


def test_the_treasury_owns_the_nft_and_needs_no_grant():
    """069 v4: owner() IS the signer; no permissions(...) read ever happens."""
    rpc = AccountRpc()
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _outer(_erc20_inner()), holder=TREASURY, rpc=rpc)
    assert d.allowed is True, d.reason
    assert abi.selector("permissions(address,address)") not in rpc.calls


def test_a_non_owner_is_refused_whatever_grant_it_holds():
    """There is no grant path: a signer that is not the NFT's owner is refused before any
    permission could be consulted (a permissioned caller is never accepted)."""
    rpc = AccountRpc(owner=OWNER)
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _outer(_erc20_inner()), holder=TREASURY, rpc=rpc)
    assert d.allowed is False and "does not own" in d.reason, d.reason
    assert abi.selector("permissions(address,address)") not in rpc.calls


def test_the_account_paying_its_owner_is_an_ordinary_send():
    """The treasury owns the NFT, so the account paying the treasury is the owner taking out
    its own funds: judged like any other transfer (no operator rule, 069 v4)."""
    intent = _via(_erc20_intent(to=TREASURY))
    deltas = _erc20_deltas(holder_transfers=((USDC.lower(), TREASURY.lower(), 250_000),),
                           sender_moved=(USDC.lower() + ":0xddf252ad",))
    d = _run(intent, deltas, _outer(_erc20_inner()), holder=TREASURY)
    assert d.allowed is True and d.lane == "autonomous", d.reason


# --- parity: the existing shapes, with and without via_account ----------------------------

def _nft_case():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=NFT, amount_raw=0, max_spend_usd=5.0,
                               idempotency_key="k", is_nft_op=True, nft_out=((NFT, "erc721", 42, 1),))
    deltas = Deltas(ok=True, native_delta=0, gas_used=90_000,
                    holder_nft_out=((NFT.lower(), "erc721", TO.lower(), 42, 1),))
    return intent, deltas, {"to": NFT, "data": "0x42842e0e", "value": 0}


def _undeclared_nft_case():
    intent, deltas, inner = _nft_case()
    return intent, Deltas(ok=True, gas_used=90_000, holder_nft_out=(
        *deltas.holder_nft_out, (NFT.lower(), "erc721", TO.lower(), 43, 1))), inner


def _registration_case():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=REGISTRY, amount_raw=0,
                               max_spend_usd=50.0, idempotency_key="k", is_registration=True,
                               expected_registry=REGISTRY)
    deltas = Deltas(ok=True, gas_used=300_000,
                    holder_nft_in=((REGISTRY.lower(), "erc721", ZERO, 42, 1),))
    return intent, deltas, {"to": REGISTRY, "data": "0x1aa3a008", "value": 0}


def _claim_case():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=ESCROW, amount_raw=0, max_spend_usd=5.0,
                               idempotency_key="k", is_claim=True, min_native_inflow_wei=10 ** 15)
    deltas = Deltas(ok=True, native_delta=2 * 10 ** 15, gas_used=80_000)
    return intent, deltas, {"to": ESCROW, "data": "0x4e71d92d", "value": 0}


def _erc20_case():
    return _erc20_intent(), _erc20_deltas(), _erc20_inner()


def _erc20_overspend_case():
    return _erc20_intent(), _erc20_deltas(token_deltas={USDC: -500_000}), _erc20_inner()


@pytest.mark.parametrize("case", [_erc20_case, _erc20_overspend_case, _nft_case, _undeclared_nft_case,
                                  _registration_case, _claim_case])
def test_existing_shapes_give_identical_verdicts_through_an_account(case):
    intent, deltas, inner = case()
    direct_tx = dict(inner, chainId=4663, nonce=5, gas=200_000, maxFeePerGas=10 ** 8)
    direct = _run(intent, deltas, direct_tx, holder=ACCOUNT)
    via = _run(_via(intent), deltas, _outer(inner), holder=TREASURY)
    assert (via.allowed, via.lane, via.amount_usd, via.agent_id) == \
        (direct.allowed, direct.lane, direct.amount_usd, direct.agent_id), (direct.reason, via.reason)
    assert via.reason == direct.reason


def test_the_parity_cases_are_not_all_refusals():
    """A parity test over refusals only would prove nothing about the happy path."""
    allowed = [c for c in (_erc20_case, _nft_case, _registration_case, _claim_case)
               if _run(c()[0], c()[1], dict(c()[2], chainId=4663, nonce=5, gas=200_000,
                                            maxFeePerGas=10 ** 8), holder=ACCOUNT).allowed]
    assert len(allowed) == 4


# --- W1 (G0.2): the ERC-2771 forwarder path and nested account calls ---------------------
# Measured on a 4663 fork 2026-09-29: AccountV3 trusts FORWARDER; an operator transaction to
# it carrying aggregate3([(account, false, execute(attacker, 0.05 ETH, "", 0))]) with 1 wei
# of value was authorized on the autonomous lane as a treasury send, and replaying it drained
# 0.05 ETH from the account. Every test below pins one half of the fix.

FORWARDER = "0xcA1167915584462449EE5b4Ea51c37fE81eCDCCD"
ATTACKER = "0x000000000000000000000000000000000000bEEF"


def _aggregate3(calls):
    """Multicall3 ``aggregate3((address,bool,bytes)[])`` — the forwarder's shape (core abi has no tuples)."""
    from eth_abi import encode
    return abi.selector("aggregate3((address,bool,bytes)[])") + encode(
        ["(address,bool,bytes)[]"], [[(a, f, bytes(d)) for a, f, d in calls]]).hex()


def _g0_2_tx(value=1, to=FORWARDER):
    inner = erc6551.encode_execute(ATTACKER, 5 * 10 ** 16, "0x", 0)
    return {"to": to, "value": value, "chainId": 4663, "nonce": 5, "gas": 200_000,
            "maxFeePerGas": 10 ** 8, "data": _aggregate3([(ACCOUNT, False, bytes.fromhex(inner[2:]))])}


def _g0_2_intent(value=1, to=FORWARDER):
    return tx_guard.TxIntent(chain="robinhood", token=None, to=to, amount_raw=value,
                             max_spend_usd=5.0, idempotency_key="g0-2")


def _native_deltas(wei=1):
    return Deltas(ok=True, native_delta=-wei, gas_used=60_000)


@pytest.mark.parametrize("value", [0, 1])
def test_g0_2_the_forwarder_transaction_refuses(value):
    """The exact G0.2 shape (value 0 and 1 wei). The simulation is the one that let it
    through: a clean 1-wei treasury outflow."""
    d = _run(_g0_2_intent(value), _native_deltas(max(value, 1)), _g0_2_tx(value), holder=TREASURY)
    assert d.allowed is False and d.lane == "refuse", d.reason
    assert "ERC-2771 forwarder" in d.reason


def test_the_static_forwarder_pin_refuses_any_calldata():
    """Even an innocuous-looking call to the pinned forwarder refuses: no calldata to it
    can be judged, because it acts AS the signer on whatever it forwards to."""
    tx = {"to": FORWARDER.lower(), "value": 1, "chainId": 4663, "data": _aggregate3([])}
    d = _run(_g0_2_intent(to=FORWARDER.lower()), _native_deltas(), tx, holder=TREASURY)
    assert d.allowed is False and "ERC-2771 forwarder" in d.reason


@pytest.mark.parametrize("wrap", [
    # a nested execute inside ANY multicall, not only the pinned forwarder
    lambda inner: _aggregate3([(ACCOUNT, False, bytes.fromhex(inner[2:]))]),
    # two levels deep
    lambda inner: _aggregate3([(TO, False, bytes.fromhex(_aggregate3(
        [(ACCOUNT, False, bytes.fromhex(inner[2:]))])[2:]))]),
    # packed at an unaligned offset
    lambda inner: "0xdeadbeef" + "ab" + inner[2:],
])
@pytest.mark.parametrize("inner", [
    erc6551.encode_execute(ATTACKER, 1, "0x", 0),
    _set_permissions([ATTACKER], [True]),
    erc6551.encode_lock(10 ** 10),
    abi.selector("executeBatch((address,uint256,bytes,uint8)[])") + "00" * 32,
])
def test_a_nested_account_call_refuses_at_any_depth(wrap, inner):
    multicall = "0x" + "77" * 20
    tx = {"to": multicall, "value": 1, "chainId": 4663, "data": wrap(inner)}
    d = _run(_g0_2_intent(to=multicall), _native_deltas(), tx, holder=TREASURY)
    assert d.allowed is False and "via_account" in d.reason, d.reason


def test_a_plain_call_with_no_account_selector_is_untouched():
    multicall = "0x" + "77" * 20
    tx = {"to": multicall, "value": 1, "chainId": 4663, "nonce": 5, "gas": 200_000,
          "maxFeePerGas": 10 ** 8, "data": _aggregate3([(TO, False, b"\x12\x34\x56\x78")])}
    d = _run(_g0_2_intent(to=multicall), _native_deltas(), tx, holder=TREASURY)
    assert d.allowed is True, d.reason


def test_a_deploy_init_code_is_not_deep_scanned():
    assert tx_guard._account_call_refusal(
        {"to": None, "data": "0x6080" + erc6551.EXECUTE_SELECTOR[2:]}, deep=False) is None
    assert tx_guard._account_call_refusal(
        {"to": None, "data": "0x6080" + erc6551.EXECUTE_SELECTOR[2:]}) is not None


def test_embedded_selector_scan_is_byte_aligned():
    sel = erc6551.EXECUTE_SELECTOR[2:]
    assert erc6551.embedded_account_selector("0x" + "a" + sel + "a") is None      # nibble offset
    assert erc6551.embedded_account_selector("0x" + "aa" + sel) == "execute"
    assert erc6551.embedded_account_selector(None) is None



def test_execute_nested_is_the_tokenbound_selector_and_is_scanned():
    # NestedAccountExecutor.executeNested(address,uint256,bytes,uint8,ERC6551AccountInfo[]) with
    # ERC6551AccountInfo = (bytes32 salt, address tokenContract, uint256 tokenId): 0x1fb1ecf0.
    assert erc6551.ADMIN_SELECTORS.get("0x1fb1ecf0") == "executeNested"
    assert erc6551.ACCOUNT_CALL_SELECTORS.get("0x1fb1ecf0") == "executeNested"
    assert erc6551.embedded_account_selector("0x" + "aa" + "1fb1ecf0") == "executeNested"
    assert tx_guard._account_call_refusal({"to": ACCOUNT, "data": "0x1fb1ecf0" + "00" * 64}) is not None


def test_a_plain_call_reads_no_account(monkeypatch):
    """069 v4: there are no bound accounts, so a plain call makes no account read."""
    rpc = AccountRpc()
    tx = {"to": TO, "value": 10 ** 15, "chainId": 4663, "nonce": 5, "gas": 21_000,
          "maxFeePerGas": 10 ** 8, "data": "0x"}
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=TO, amount_raw=10 ** 15,
                               max_spend_usd=50.0, idempotency_key="n")
    d = _run(intent, _native_deltas(10 ** 15), tx, holder=TREASURY, rpc=rpc)
    assert d.allowed is True and rpc.forwarder_reads == [] and rpc.calls == []


# the same rules on the INNER call of an account transaction

def test_an_account_inner_call_may_not_target_the_forwarder():
    inner = {"to": FORWARDER, "data": _aggregate3([]), "value": 0}
    d = _run(_via(_erc20_intent(to=FORWARDER)), _erc20_deltas(), _outer(inner), holder=TREASURY)
    assert d.allowed is False and "ERC-2771 forwarder" in d.reason


def test_an_account_inner_call_may_not_nest_an_account_call_at_depth():
    nested = _aggregate3([(ACCOUNT, False, bytes.fromhex(erc6551.encode_execute(TO, 1, "0x")[2:]))])
    inner = {"to": "0x" + "77" * 20, "data": nested, "value": 0}
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _outer(inner), holder=TREASURY)
    assert d.allowed is False and "nested execute" in d.reason


def test_an_account_inner_destination_the_account_trusts_is_refused_by_the_live_read():
    other_fwd = "0x" + "99" * 20
    inner = {"to": other_fwd, "data": "0x12345678", "value": 0}
    d = _run(_via(_erc20_intent(to=other_fwd)), _erc20_deltas(), _outer(inner), holder=TREASURY,
             rpc=AccountRpc(forwarders=(other_fwd,)))
    assert d.allowed is False and "forwarder" in d.reason


# --- W8: the approve-spend-reset batch ---------------------------------------------------
# executeBatch([token.approve(S, X), S.spend(...), token.approve(S, 0)]) through the account.
# Each leg is checked; the spend leg is the inner call; the FINAL allowance must be 0.

ROUTER = "0x7777777777777777777777777777777777777777"


def _approve(spender, amount):
    return abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}], [spender, amount])


def _batch_tx(legs):
    return {"to": ACCOUNT, "value": 0, "chainId": 4663, "nonce": 5, "gas": 400_000,
            "maxFeePerGas": 10 ** 8, "data": erc6551.encode_execute_batch(legs)}


def _good_legs(grant=250_000, reset=0, spend_to=ROUTER, spender=ROUTER):
    return [(USDC, 0, _approve(spender, grant), 0), (spend_to, 0, "0x12345678" + "00" * 32, 0),
            (USDC, 0, _approve(spender, reset), 0)]


def _batch_intent(**kw):
    base = dict(chain="base", token=USDC, to=ROUTER, amount_raw=250_000, max_spend_usd=5.0,
                idempotency_key="b", via_account=ACCOUNT, via_account_state=STATE, via_account_batch=True)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _batch_deltas(after=0, **kw):
    base = dict(ok=True, native_delta=0, token_deltas={USDC: -250_000},
                allowance_deltas={(USDC, ROUTER): 0}, allowance_after={(USDC, ROUTER): after},
                holder_transfers=((USDC.lower(), ROUTER.lower(), 250_000),),
                holder_approvals=((USDC.lower(), ROUTER.lower(), 250_000), (USDC.lower(), ROUTER.lower(), 0)),
                gas_used=150_000)
    base.update(kw)
    return Deltas(**base)


def test_batch_encoding_round_trips():
    legs = _good_legs()
    assert [(t.lower(), v, d, o) for t, v, d, o in erc6551.decode_execute_batch(
        erc6551.encode_execute_batch(legs))] == [(t.lower(), v, d, o) for t, v, d, o in legs]


def test_a_clean_approve_spend_reset_batch_is_authorized_and_judged_on_the_spend_leg():
    calls = []
    d = _run(_batch_intent(), _batch_deltas(), _batch_tx(_good_legs()), holder=TREASURY, sim_calls=calls)
    assert d.allowed is True and d.lane == "autonomous", d.reason
    (kw,) = calls
    assert kw["tx"]["to"] == ACCOUNT and kw["holder"] == ACCOUNT and kw["sender"] == TREASURY
    assert ROUTER in kw["spenders"]          # the final allowance is measured


@pytest.mark.parametrize("after, needle", [
    (1, "must end at exactly 0"),
    (None, "final allowance"),
])
def test_the_final_allowance_must_be_measured_zero(after, needle):
    deltas = _batch_deltas(after=after or 0)
    if after is None:
        deltas = _batch_deltas(allowance_after={})
    d = _run(_batch_intent(), deltas, _batch_tx(_good_legs()), holder=TREASURY)
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("legs, needle", [
    (_good_legs()[:2], "exactly 3 legs"),
    (_good_legs() + [(USDC, 0, _approve(ROUTER, 0), 0)], "exactly 3 legs"),
    (_good_legs(reset=1), "not 0"),
    (_good_legs(grant=250_001), "no larger than"),
    (_good_legs(grant=0), "must be > 0"),
    (_good_legs(spender=TO), "same address"),
    (_good_legs(spend_to=TO), "not the approved spender"),
    ([(USDC, 0, _approve(ROUTER, 1), 1)] + _good_legs()[1:], "operation 1"),
    ([_good_legs()[0], (ACCOUNT, 0, "0x", 0), _good_legs()[2]], "token-bound account itself"),
    ([_good_legs()[0], ("0xcA1167915584462449EE5b4Ea51c37fE81eCDCCD", 0, "0x", 0), _good_legs()[2]],
     "ERC-2771"),
    ([_good_legs()[0], (ROUTER, 0, erc6551.encode_execute(TO, 1, "0x"), 0), _good_legs()[2]],
     "nested execute"),
    ([("0x" + "88" * 20, 0, _approve(ROUTER, 1), 0)] + _good_legs()[1:], "open with token.approve"),
])
def test_batch_leg_refusals(legs, needle):
    d = _run(_batch_intent(), _batch_deltas(), _batch_tx(legs), holder=TREASURY)
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("kw, needle", [
    (dict(via_account=None, via_account_state=None), "declare `via_account`"),
    (dict(is_allowance_op=True), "token SPEND"),
    (dict(expected_allowance_grants=((USDC, ROUTER, 1),)), "token SPEND"),
    (dict(token=None), "token SPEND"),
])
def test_batch_intent_refusals(kw, needle):
    d = _run(_batch_intent(**kw), _batch_deltas(), _batch_tx(_good_legs()), holder=TREASURY)
    assert d.allowed is False and needle in d.reason, d.reason


def test_an_undeclared_execute_batch_is_still_account_admin():
    d = _run(_via(_erc20_intent()), _erc20_deltas(), _batch_tx(_good_legs()), holder=TREASURY)
    assert d.allowed is False and "executeBatch" in d.reason


def test_a_batch_overspend_refuses_on_the_measured_outflow():
    d = _run(_batch_intent(), _batch_deltas(token_deltas={USDC: -250_001}), _batch_tx(_good_legs()),
             holder=TREASURY)
    assert d.allowed is False and "exceeds the declared amount" in d.reason
