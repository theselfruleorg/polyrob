"""050 §7.2 / §7.7: the token-bound account, pinned by bytecode.

The fixture is MEASURED (2026-09-18, the registry's own answer on Ethereum and on
4663): ``account(AccountV3, 0, 4663, Normies 0x9eb6…2438, 232)`` =
``0xe0f47c083b28c76124129786cbd02a4489b86ec4``. Hashing the tuple instead of using
the RAW salt gives a plausible wrong address — the test pins both facts.
"""
import hashlib

import pytest

from core.wallet import abi, erc6551

NORMIES = "0x9eb6e2025b64f340691e424b7fe7022ffde12438"
FIXTURE = "0xe0f47c083b28c76124129786cbd02a4489b86ec4"
ACCOUNT = "0x" + "a1" * 20
SPENDER = "0x" + "b2" * 20
TOKEN = "0x" + "c3" * 20
NFT = "0x" + "d4" * 20


# --- address math -------------------------------------------------------------

def test_account_address_reproduces_the_measured_registry_answer():
    assert erc6551.account_address(4663, NORMIES, 232).lower() == FIXTURE


def test_the_hashed_tuple_salt_is_wrong():
    from eth_utils import keccak
    hashed = int.from_bytes(keccak(
        (0).to_bytes(32, "big") + (4663).to_bytes(32, "big") + bytes(12)
        + bytes.fromhex(NORMIES[2:]) + (232).to_bytes(32, "big")), "big")
    assert erc6551.account_address(4663, NORMIES, 232, salt=hashed).lower() != FIXTURE


def _clone_code(impl=erc6551.ACCOUNT_V3_IMPL, chain_id=4663, contract=NORMIES, token_id=232):
    return ("0x363d3d373d3d3d363d73" + impl[2:].lower() + "5af43d82803e903d91602b57fd5bf3"
            + "00" * 32 + chain_id.to_bytes(32, "big").hex() + "00" * 12 + contract[2:].lower()
            + token_id.to_bytes(32, "big").hex())


class FakeRpc:
    def __init__(self, code=None, calls=None, logs=None, head=1000):
        self.code = code or {}
        self.calls = calls or {}
        self.logs = logs or []
        self.head = head
        self.log_queries = []

    def __call__(self, method, params):
        if method == "eth_getCode":
            return self.code.get(params[0].lower(), "0x")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            q = params[0]
            self.log_queries.append((int(q["fromBlock"], 16), int(q["toBlock"], 16)))
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            return [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi]
        if method == "eth_call":
            key = (params[0]["to"].lower(), params[0]["data"][:10].lower())
            v = self.calls.get(key)
            if isinstance(v, Exception):
                raise v
            return v
        raise AssertionError(method)


def test_read_implementation_is_the_erc1167_footer_not_a_slot():
    rpc = FakeRpc(code={ACCOUNT: _clone_code()})
    assert erc6551.read_implementation(rpc, ACCOUNT).lower() == erc6551.ACCOUNT_V3_IMPL.lower()
    salt, chain_id, contract, tid = erc6551.read_footer(rpc, ACCOUNT)
    assert (salt, chain_id, contract.lower(), tid) == (0, 4663, NORMIES, 232)


def test_read_implementation_refuses_a_non_clone():
    rpc = FakeRpc(code={ACCOUNT: "0x6080604052"})
    with pytest.raises(erc6551.Erc6551Error):
        erc6551.read_implementation(rpc, ACCOUNT)


# --- pins -----------------------------------------------------------------------

def test_verify_refuses_missing_and_changed_code(monkeypatch):
    good = b"\x60\x80"
    monkeypatch.setattr(erc6551, "CODE_SHA256", {TOKEN: hashlib.sha256(good).hexdigest()})
    erc6551.verify(FakeRpc(code={TOKEN: "0x" + good.hex()}))
    with pytest.raises(erc6551.Erc6551Error, match="NO code"):
        erc6551.verify(FakeRpc())
    with pytest.raises(erc6551.Erc6551Error, match="un-reviewed"):
        erc6551.verify(FakeRpc(code={TOKEN: "0x6081"}))


def test_the_pins_are_the_measured_hashes():
    assert erc6551.CODE_SHA256[erc6551.REGISTRY.lower()].startswith("d7df9983")
    assert erc6551.CODE_SHA256[erc6551.ACCOUNT_V3_IMPL.lower()].startswith("0b890bf4")
    assert erc6551.ACCOUNT_PROXY.lower() not in erc6551.CODE_SHA256  # recorded, never used


# --- encoders -------------------------------------------------------------------

def test_execute_round_trips_and_admin_selectors_are_named():
    data = erc6551.encode_execute(TOKEN, 5, "0xdeadbeef")
    assert erc6551.decode_execute(data) == (erc6551._checksum(bytes.fromhex(TOKEN[2:])), 5, "0xdeadbeef", 0)
    assert erc6551.admin_selector_name(data) is None
    assert erc6551.admin_selector_name(erc6551.encode_execute(TOKEN, 0, "0x", op=1)) == "execute(operation=1)"
    assert erc6551.admin_selector_name(erc6551.encode_lock(123)) == "lock"
    grant = abi.encode_call("setPermissions", [{"type": "address[]"}, {"type": "bool[]"}], [[SPENDER], [True]])
    assert erc6551.admin_selector_name(grant) == "setPermissions"
    # 069 v4: core has no grant encoder and no permission read
    assert not hasattr(erc6551, "encode_set_permissions") and not hasattr(erc6551, "read_permission")
    for sig, name in [("setOverrides(bytes4[],address[])", "setOverrides"),
                      ("upgradeToAndCall(address,bytes)", "upgradeToAndCall"),
                      ("executeBatch((address,uint256,bytes,uint8)[])", "executeBatch")]:
        assert erc6551.admin_selector_name(abi.selector(sig) + "00" * 32) == name


def test_create_account_calldata_targets_the_pinned_impl_with_the_raw_salt():
    data = erc6551.encode_create_account(4663, NORMIES, 232)
    assert data.startswith(abi.selector("createAccount(address,bytes32,uint256,address,uint256)"))
    impl, salt, chain_id, contract, tid = abi.decode(
        [{"type": "address"}, {"type": "bytes32"}, {"type": "uint256"}, {"type": "address"},
         {"type": "uint256"}], data[10:])
    assert impl.lower() == erc6551.ACCOUNT_V3_IMPL.lower()
    assert int(salt, 16) == 0 and chain_id == 4663 and tid == 232


#: W13 (core handoff): the account's own signature CHECKS are view reads, not signing helpers.
#: Every other public name with "sign" in it still fails — adding one needs a reason here.
_SIGNATURE_READS = {"read_is_valid_signer", "read_is_valid_signature",
                    "IS_VALID_SIGNER_MAGIC", "IS_VALID_SIGNATURE_MAGIC"}


def test_no_signing_helper_is_exposed():
    names = {n for n in dir(erc6551) if "sign" in n.lower() and not n.startswith("_")}
    assert names <= _SIGNATURE_READS, sorted(names - _SIGNATURE_READS)
    for n in names:
        obj = getattr(erc6551, n)
        assert not callable(obj) or n.startswith("read_"), n    # a read, never a signer


# --- open approvals ---------------------------------------------------------------

def _word(n):
    return "0x" + int(n).to_bytes(32, "big").hex()


def _log(block, contract, topic0, *topics, data="0x", index=0):
    return {"address": contract, "blockNumber": hex(block), "logIndex": hex(index),
            "topics": [topic0, erc6551._pad_topic(ACCOUNT), *topics], "data": data}


def _erc20(block, amount, spender=SPENDER, index=0):
    return _log(block, TOKEN, erc6551.TOPIC_APPROVAL, erc6551._pad_topic(spender),
                data=_word(amount), index=index)


def test_grant_then_revoke_is_empty_and_grant_then_regrant_is_one_row():
    rpc = FakeRpc(logs=[_erc20(10, 5), _erc20(11, 0)])
    assert erc6551.open_approvals(rpc, ACCOUNT, 0, live=False) == []
    rpc = FakeRpc(logs=[_erc20(10, 5), _erc20(12, 9)])
    rows = erc6551.open_approvals(rpc, ACCOUNT, 0, live=False)
    assert len(rows) == 1 and rows[0].amount == 9 and rows[0].kind == "erc20"


def test_order_is_by_block_then_log_index_not_by_arrival():
    rpc = FakeRpc(logs=[_erc20(20, 0, index=3), _erc20(20, 7, index=1)])
    assert erc6551.open_approvals(rpc, ACCOUNT, 0, live=False) == []


def test_operator_and_erc721_rows():
    logs = [
        _log(5, NFT, erc6551.TOPIC_APPROVAL_FOR_ALL, erc6551._pad_topic(SPENDER), data=_word(1)),
        _log(6, NFT, erc6551.TOPIC_APPROVAL, erc6551._pad_topic(SPENDER), _word(7)),
        _log(7, NFT, erc6551.TOPIC_APPROVAL, erc6551._pad_topic(SPENDER), _word(8)),
        _log(8, NFT, erc6551.TOPIC_APPROVAL, erc6551._pad_topic("0x" + "00" * 20), _word(8)),
    ]
    rows = erc6551.open_approvals(FakeRpc(logs=logs), ACCOUNT, 0, live=False)
    kinds = sorted((r.kind, r.token_id) for r in rows)
    assert kinds == [("erc721", 7), ("operator", None)]
    logs.append(_log(9, NFT, erc6551.TOPIC_APPROVAL_FOR_ALL, erc6551._pad_topic(SPENDER), data=_word(0)))
    rows = erc6551.open_approvals(FakeRpc(logs=logs), ACCOUNT, 0, live=False)
    assert [(r.kind, r.token_id) for r in rows] == [("erc721", 7)]


def test_live_confirmation_drops_spent_allowances_and_keeps_failed_reads():
    """OZ 5 lowers an allowance in transferFrom WITHOUT an Approval event; a failed
    live read must keep the row (a missed approval is the dangerous error)."""
    allowance = abi.selector("allowance(address,address)")
    rpc = FakeRpc(logs=[_erc20(10, 5)], calls={(TOKEN, allowance): _word(0)})
    assert erc6551.open_approvals(rpc, ACCOUNT, 0) == []
    rpc = FakeRpc(logs=[_erc20(10, 5)], calls={(TOKEN, allowance): _word(3)})
    rows = erc6551.open_approvals(rpc, ACCOUNT, 0)
    assert rows[0].amount == 3 and rows[0].verified
    rpc = FakeRpc(logs=[_erc20(10, 5)], calls={(TOKEN, allowance): RuntimeError("rpc down")})
    rows = erc6551.open_approvals(rpc, ACCOUNT, 0)
    assert len(rows) == 1 and rows[0].verified is False


def test_an_nft_that_left_the_account_takes_its_approval_with_it():
    owner_of = abi.selector("ownerOf(uint256)")
    rpc = FakeRpc(logs=[_log(6, NFT, erc6551.TOPIC_APPROVAL, erc6551._pad_topic(SPENDER), _word(7))],
                  calls={(NFT, owner_of): _word(int(SPENDER, 16))})
    assert erc6551.open_approvals(rpc, ACCOUNT, 0) == []


def test_the_scan_is_chunked_and_a_bad_page_refuses():
    rpc = FakeRpc(head=120_000)
    erc6551.open_approvals(rpc, ACCOUNT, 0, step=50_000, live=False)
    assert rpc.log_queries == [(0, 49_999), (50_000, 99_999), (100_000, 120_000)]

    class Broken(FakeRpc):
        def __call__(self, method, params):
            if method == "eth_getLogs":
                return None
            return super().__call__(method, params)
    with pytest.raises(erc6551.Erc6551Error, match="refusing"):
        erc6551.open_approvals(Broken(), ACCOUNT, 0, live=False)


# --- W3: Permit2 in the approval table ------------------------------------------------------
# A Permit2 allowance is held BY Permit2, keyed (owner, token, spender), and survives a sale.
# "Empty open-approval table" is the sale-safety claim, so the scan must read it.

P2 = erc6551.PERMIT2.lower()
P2_ALLOWANCE = abi.selector("allowance(address,address,address)")


def _p2(block, amount, expiration=2_000_000_000, topic0=None, emitter=None, spender=SPENDER, index=0):
    return _log(block, emitter or erc6551.PERMIT2, topic0 or erc6551.TOPIC_PERMIT2_APPROVAL,
                erc6551._pad_topic(TOKEN), erc6551._pad_topic(spender),
                data="0x" + int(amount).to_bytes(32, "big").hex() + int(expiration).to_bytes(32, "big").hex(),
                index=index)


def _lockdown(block, spender=SPENDER):
    return {"address": erc6551.PERMIT2, "blockNumber": hex(block), "logIndex": "0x0",
            "topics": [erc6551.TOPIC_PERMIT2_LOCKDOWN, erc6551._pad_topic(ACCOUNT)],
            "data": "0x" + erc6551._pad_topic(TOKEN)[2:] + erc6551._pad_topic(spender)[2:]}


def _p2_word(amount, expiration, nonce=0):
    return "0x" + "".join(int(v).to_bytes(32, "big").hex() for v in (amount, expiration, nonce))


class ClockRpc(FakeRpc):
    def __init__(self, *a, now=1_900_000_000, **kw):
        super().__init__(*a, **kw)
        self.now = now

    def __call__(self, method, params):
        if method == "eth_getBlockByNumber":
            if self.now is None:
                raise RuntimeError("no block")
            return {"timestamp": hex(self.now)}
        return super().__call__(method, params)


def test_approval_kinds_names_permit2():
    """The agent-NFT package calls the table complete only when this names permit2."""
    assert erc6551.APPROVAL_KINDS == ("erc20", "erc721", "operator", "permit2")


def test_the_scan_asks_for_the_permit2_topics():
    seen = []

    class Spy(FakeRpc):
        def __call__(self, method, params):
            if method == "eth_getLogs":
                seen.append(params[0]["topics"][0])
            return super().__call__(method, params)
    erc6551.open_approvals(Spy(), ACCOUNT, 0, live=False)
    assert {erc6551.TOPIC_PERMIT2_APPROVAL, erc6551.TOPIC_PERMIT2_PERMIT,
            erc6551.TOPIC_PERMIT2_LOCKDOWN} <= set(seen[0])


def test_permit2_grant_permit_revoke_and_lockdown():
    rows = erc6551.open_approvals(FakeRpc(logs=[_p2(10, 5)]), ACCOUNT, 0, live=False)
    assert [(r.kind, r.contract.lower(), r.spender.lower(), r.amount, r.expiration) for r in rows] == \
        [("permit2", TOKEN, SPENDER, 5, 2_000_000_000)]
    permit = _p2(11, 9, topic0=erc6551.TOPIC_PERMIT2_PERMIT)
    rows = erc6551.open_approvals(FakeRpc(logs=[_p2(10, 5), permit]), ACCOUNT, 0, live=False)
    assert rows[0].amount == 9
    assert erc6551.open_approvals(FakeRpc(logs=[_p2(10, 5), _p2(11, 0)]), ACCOUNT, 0, live=False) == []
    assert erc6551.open_approvals(FakeRpc(logs=[_p2(10, 5), _lockdown(12)]), ACCOUNT, 0, live=False) == []


def test_a_permit2_topic_from_any_other_contract_is_ignored():
    rows = erc6551.open_approvals(FakeRpc(logs=[_p2(10, 5, emitter=NFT)]), ACCOUNT, 0, live=False)
    assert rows == []


def test_permit2_live_recheck_amount_expiry_and_fail_closed():
    live = {(P2, P2_ALLOWANCE): _p2_word(3, 2_000_000_000)}
    rows = erc6551.open_approvals(ClockRpc(logs=[_p2(10, 5)], calls=live), ACCOUNT, 0)
    assert rows[0].amount == 3 and rows[0].verified and rows[0].expiration == 2_000_000_000
    spent = {(P2, P2_ALLOWANCE): _p2_word(0, 2_000_000_000)}
    assert erc6551.open_approvals(ClockRpc(logs=[_p2(10, 5)], calls=spent), ACCOUNT, 0) == []
    expired = {(P2, P2_ALLOWANCE): _p2_word(3, 1_800_000_000)}
    assert erc6551.open_approvals(ClockRpc(logs=[_p2(10, 5)], calls=expired), ACCOUNT, 0) == []
    broken = {(P2, P2_ALLOWANCE): RuntimeError("rpc down")}
    rows = erc6551.open_approvals(ClockRpc(logs=[_p2(10, 5)], calls=broken), ACCOUNT, 0)
    assert len(rows) == 1 and rows[0].verified is False
    no_clock = ClockRpc(logs=[_p2(10, 5)], calls=live, now=None)
    rows = erc6551.open_approvals(no_clock, ACCOUNT, 0)
    assert len(rows) == 1 and rows[0].verified is False


def test_revoke_encoders():
    assert erc6551.encode_permit2_revoke(TOKEN, SPENDER)[:10] == \
        abi.selector("approve(address,address,uint160,uint48)")
    enc = erc6551.encode_erc721_revoke(7)
    assert enc[:10] == abi.selector("approve(address,uint256)")
    to, tid = abi.decode([{"type": "address"}, {"type": "uint256"}], enc[10:])
    assert int(to, 16) == 0 and tid == 7


# --- W13: token() / isValidSigner / isValidSignature --------------------------------------

def _bytes4(magic):
    return magic + "00" * 28


def test_read_token_is_the_accounts_own_answer():
    word = "0x" + (4663).to_bytes(32, "big").hex() + "00" * 12 + NFT[2:] + (7).to_bytes(32, "big").hex()
    rpc = FakeRpc(calls={(ACCOUNT, abi.selector("token()")): word})
    chain_id, contract, token_id = erc6551.read_token(rpc, ACCOUNT)
    assert (chain_id, contract.lower(), token_id) == (4663, NFT, 7)


@pytest.mark.parametrize("answer, expected", [
    (_bytes4(erc6551.IS_VALID_SIGNER_MAGIC), True),
    (_bytes4("0x00000000"), False),
])
def test_read_is_valid_signer(answer, expected):
    rpc = FakeRpc(calls={(ACCOUNT, abi.selector("isValidSigner(address,bytes)")): answer})
    assert erc6551.read_is_valid_signer(rpc, ACCOUNT, SPENDER) is expected


@pytest.mark.parametrize("answer, expected", [
    (_bytes4(erc6551.IS_VALID_SIGNATURE_MAGIC), True),
    (_bytes4("0xffffffff"), False),
])
def test_read_is_valid_signature(answer, expected):
    rpc = FakeRpc(calls={(ACCOUNT, abi.selector("isValidSignature(bytes32,bytes)")): answer})
    assert erc6551.read_is_valid_signature(rpc, ACCOUNT, "0x" + "11" * 32, "0x" + "22" * 65) is expected


def test_an_unreadable_signer_read_raises_never_answers_false():
    rpc = FakeRpc(calls={(ACCOUNT, abi.selector("isValidSigner(address,bytes)")): RuntimeError("down")})
    with pytest.raises(RuntimeError):
        erc6551.read_is_valid_signer(rpc, ACCOUNT, SPENDER)
    with pytest.raises(erc6551.Erc6551Error):              # "0x" / nothing: refuse to guess
        erc6551.read_token(FakeRpc(calls={(ACCOUNT, abi.selector("token()")): "0x"}), ACCOUNT)
