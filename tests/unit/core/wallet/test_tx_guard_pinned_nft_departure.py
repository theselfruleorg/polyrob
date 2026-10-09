"""069 v4 §5 rule 5 (+ rule 3) in the guard: a PINNED collection's token leaves the holder only
with the collection's live code matching its pinned runtime_sha256 and its account carrying NO
open approval (complete scan). No override for a row an NFT owner's transaction granted;
the owner may accept only a row no owner's transaction emitted (possibly fabricated). Fail closed.

Also: the collection mint and reveal shapes re-check the runtime hash (069 v4 A3)."""
from core.wallet import collection_registry, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import CODE, RUNTIME_SHA256, pin, profile

HOLDER = "0x1111111111111111111111111111111111111111"
PINNED = "0xC011000000000000000000000000000000000001"
OTHER_NFT = "0x4444444444444444444444444444444444444444"
DEST = "0x9999999999999999999999999999999999999999"
TOKEN = "0x2222222222222222222222222222222222222222"
SPENDER = "0x5555555555555555555555555555555555555555"
ACCOUNT_42 = erc6551.account_address(4663, PINNED, 42)


def _w(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


class Rpc:
    """eth_getCode (collection → CODE unless overridden, destination → EOA), eth_blockNumber,
    eth_getLogs (the approval scan), eth_call (the live confirmation of an approval)."""

    def __init__(self, *, code=CODE, logs=(), allowance=7, broken=None):
        self.code, self.logs, self.allowance, self.broken = code, list(logs), allowance, broken
        self.calls = []

    def __call__(self, method, params):
        self.calls.append(method)
        if self.broken == method:
            raise RuntimeError(f"{method} down")
        if method == "eth_getCode":
            return self.code if str(params[0]).lower() == PINNED.lower() else "0x"
        if method == "eth_blockNumber":
            return hex(0x200)
        if method == "eth_getLogs":
            return list(self.logs)
        if method == "eth_call":
            return "0x" + f"{self.allowance:064x}"
        raise AssertionError(method)


def _erc20_approval_log(account=ACCOUNT_42, amount=7):
    return {"address": TOKEN, "blockNumber": hex(0x150), "logIndex": "0x0",
            "topics": [erc6551.TOPIC_APPROVAL, _w(account), _w(SPENDER)],
            "data": "0x" + f"{amount:064x}"}


def _run(contract, rpc, *, dest=DEST, token_id=42, ctx=None, forged=lambda c, t: False,
         accepted=()):
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=contract, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="k", is_nft_op=True,
                               nft_out=((contract, "erc721", token_id, 1),),
                               accepted_unattributed_approvals=tuple(accepted))
    deltas = Deltas(ok=True, native_delta=0, gas_used=90_000,
                    holder_nft_out=((contract.lower(), "erc721", dest.lower(), token_id, 1),))
    tx = {"to": contract, "data": "0x23b872dd", "value": 0, "chainId": 4663, "nonce": 1,
          "gas": 120_000, "maxFeePerGas": 10 ** 8}
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=ctx, simulate_fn=lambda **_: deltas, price_fn=lambda c, a: 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=forged, account_rpc=rpc)


def test_a_pinned_token_with_no_open_approval_leaves(monkeypatch):
    pin(monkeypatch, PINNED)
    rpc = Rpc()
    d = _run(PINNED, rpc)
    assert d.allowed is True, d.reason
    assert "eth_getLogs" in rpc.calls                       # the scan ran


def test_an_open_erc20_approval_on_the_account_refuses_with_the_remedy(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, Rpc(logs=[_erc20_approval_log()]))
    assert d.allowed is False
    assert "1 open approval" in d.reason and ACCOUNT_42 in d.reason
    assert "agent_nft_revoke_all" in d.reason and "survive a transfer" in d.reason


def test_a_revoked_approval_does_not_count(monkeypatch):
    # the log says 7, the live read says 0 (spent down / revoked without an event)
    pin(monkeypatch, PINNED)
    assert _run(PINNED, Rpc(logs=[_erc20_approval_log()], allowance=0)).allowed is True


def test_an_unconfirmable_approval_still_refuses(monkeypatch):
    # the live read fails: the row is KEPT (a missed approval is the dangerous error)
    pin(monkeypatch, PINNED)
    d = _run(PINNED, Rpc(logs=[_erc20_approval_log()], broken="eth_call"))
    assert d.allowed is False and "(unconfirmed)" in d.reason


def test_an_incomplete_scan_fails_closed(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, Rpc(broken="eth_getLogs"))
    assert d.allowed is False and "did not complete" in d.reason and "never 'empty'" in d.reason


def test_a_scan_without_permit2_coverage_fails_closed(monkeypatch):
    pin(monkeypatch, PINNED)
    monkeypatch.setattr(erc6551, "APPROVAL_KINDS", ("erc20", "erc721", "operator"))
    d = _run(PINNED, Rpc())
    assert d.allowed is False and "Permit2" in d.reason


def test_the_rule_is_absolute_on_an_owner_direct_turn(monkeypatch):
    """No owner override: an owner turn is refused the same way (the remedy is one call)."""
    import types
    pin(monkeypatch, PINNED)
    monkeypatch.setattr(tx_guard, "owner_authority", lambda *a, **k: (True, False))
    monkeypatch.setattr("core.wallet.authority.turn_refusal", lambda ctx: None)
    ctx = types.SimpleNamespace(user_id="owner", session_id="s", metadata={})
    assert _run(PINNED, Rpc(), ctx=ctx).lane == "owner_direct"       # the owner lane is live
    d = _run(PINNED, Rpc(logs=[_erc20_approval_log()]), ctx=ctx)
    assert d.allowed is False and "agent_nft_revoke_all" in d.reason


def test_a_changed_collection_runtime_refuses(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, Rpc(code="0x6080deadbeef"))
    assert d.allowed is False and "not the pinned" in d.reason


def test_an_unreadable_collection_runtime_refuses(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, Rpc(code="0x"))
    assert d.allowed is False and "could not be read" in d.reason


def test_an_unpinned_collection_is_not_judged_by_this_rule(monkeypatch):
    pin(monkeypatch, PINNED)
    rpc = Rpc(logs=[_erc20_approval_log(account=erc6551.account_address(4663, OTHER_NFT, 42))])
    assert _run(OTHER_NFT, rpc).allowed is True
    assert "eth_getLogs" not in rpc.calls


def test_every_pinned_account_version_is_scanned(monkeypatch):
    raw = profile(PINNED)
    raw["accounts"].append({"registry": erc6551.REGISTRY, "implementation": erc6551.ACCOUNT_V3_IMPL,
                            "salt": 7})
    pin(monkeypatch, raw)
    second = erc6551.account_address(4663, PINNED, 42, salt=7)
    d = _run(PINNED, Rpc(logs=[_erc20_approval_log(account=second)]))
    # the fake returns the same log for both scans; the refusal names the FIRST account scanned
    assert d.allowed is False and "open approval" in d.reason


def test_the_helper_reads_the_registry_fail_closed(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("bad file")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    why = tx_guard._pinned_nft_departure_refusal(
        "robinhood", [(PINNED, "erc721", DEST, 42, 1)], Rpc())
    assert "cannot be trusted" in why


def test_runtime_refusal_helper():
    p = collection_registry.parse({"profiles": [profile(PINNED)]})[0]
    assert p.runtime_sha256 == RUNTIME_SHA256
    assert collection_registry.runtime_refusal(Rpc(), p) is None
    assert "not the pinned" in collection_registry.runtime_refusal(Rpc(code="0x00"), p)
    assert "could not be read" in collection_registry.runtime_refusal(Rpc(broken="eth_getCode"), p)


# ---- a fabricated row (any contract can emit an Approval naming the account) ---------------

GRANT_TX = "0x" + "9a" * 32
STRANGER = "0x7777777777777777777777777777777777777777"
KEY = f"erc20:{TOKEN.lower()}:{SPENDER.lower()}:"


class SentBy(Rpc):
    """The approval log carries a tx hash; the tx was SENT by ``sender``."""

    def __init__(self, sender, **kw):
        log = dict(_erc20_approval_log(), transactionHash=GRANT_TX)
        super().__init__(logs=[log], **kw)
        self.sender = sender

    def __call__(self, method, params):
        if method == "eth_getTransactionByHash":
            self.calls.append(method)
            return {"hash": params[0], "from": self.sender}
        return super().__call__(method, params)


def test_an_unattributed_row_refuses_and_names_the_key_the_owner_may_accept(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, SentBy(STRANGER))
    assert d.allowed is False and KEY in d.reason
    assert "no transaction an owner of this NFT sent emitted it" in d.reason


def test_the_owner_may_accept_an_unattributed_row(monkeypatch):
    pin(monkeypatch, PINNED)
    assert _run(PINNED, SentBy(STRANGER), accepted=[KEY]).allowed is True


def test_a_row_the_owner_granted_can_never_be_accepted(monkeypatch):
    pin(monkeypatch, PINNED)
    d = _run(PINNED, SentBy(HOLDER), accepted=[KEY])
    assert d.allowed is False and "agent_nft_revoke_all" in d.reason and KEY not in d.reason


def test_an_unreadable_origin_may_be_accepted_but_never_counts_as_attributed(monkeypatch):
    pin(monkeypatch, PINNED)
    rpc = SentBy(STRANGER)
    rpc.sender = None

    class Broken(SentBy):
        def __call__(self, method, params):
            if method == "eth_getTransactionByHash":
                raise RuntimeError("down")
            return super().__call__(method, params)
    d = _run(PINNED, Broken(STRANGER))
    assert d.allowed is False and "origin could not be read" in d.reason
    assert _run(PINNED, Broken(STRANGER), accepted=[KEY]).allowed is True
