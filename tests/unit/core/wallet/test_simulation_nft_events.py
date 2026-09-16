"""The guard sees NON-FUNGIBLE movement — ERC-721 and ERC-1155.

⚠️ Why this file exists. `_holder_events` read `if len(topics) != 3: continue`,
and ERC-721 `Transfer` carries FOUR topics (all three parameters are indexed).
So every ERC-721 and ERC-1155 event was skipped, in both directions, and
`ApprovalForAll` — a standing claim on every token of a collection, present and
future — was completely invisible. The moment the treasury holds a collectible
(a `defi_trade.call`, a `dapp_connect` mint, an airdrop) an attacker-authored
transaction could move it out and the guard's "undeclared token" refusal would
not fire.

⚠️ THE DISCRIMINATOR IS THE TOPIC COUNT, NOT THE HASH. ERC-20 and ERC-721
`Transfer` have the IDENTICAL `topics[0]` — the canonical signature string
`Transfer(address,address,uint256)` is the same for both, so the keccak is the
same. Ditto `Approval`. Only the arity separates them, and it separates them
exactly.

⚠️ THE ERC-1155 TRAP. `TransferSingle(operator, from, to, id, value)` indexes the
OPERATOR first, so `topics[1]` is NOT the sender. A parser that reuses the
ERC-20 `topics[1] == holder` filter reads 1155 backwards: it matches a transfer
some marketplace we approved made on someone else's behalf, and MISSES the one
where our own token left. `test_erc1155_keys_the_sender_off_topic2_not_topic1`
is that bug, pinned.
"""
import pytest

from core.wallet import simulation

HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OTHER = "0x1111111111111111111111111111111111111111"
MARKET = "0x3333333333333333333333333333333333333333"
NFT = "0x4444444444444444444444444444444444444444"

ETH = 10 ** 18

# keccak of the canonical signatures (verified 2026-09-15).
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
T_APPROVAL_FOR_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"
T_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
T_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


def _word(n):
    return "0x" + f"{n:064x}"


def _raw_word(n):
    return f"{n:064x}"


def _topic_addr(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _ok(v):
    return {"status": "0x1", "returnData": _word(v)}


def _bundle(*entries):
    return [{"calls": list(entries)}]


def _sim(tx_entry):
    """Run one tx entry through the engine and return its Deltas."""
    entries = _bundle(_ok(ETH), _ok(1000), tx_entry, _ok(ETH), _ok(1000))

    def rpc(method, params, timeout=8.0):
        return entries

    return simulation.simulate(
        {"to": NFT, "data": "0x", "value": 0, "chainId": 8453},
        holder=HOLDER, chain="base", tokens=[USDC], spenders=[], rpc=rpc)


def _entry(*logs):
    return {"status": "0x1", "returnData": "0x", "logs": list(logs)}


def _erc721_transfer(contract, frm, to, token_id):
    """FOUR topics: all three parameters indexed, data empty."""
    return {"address": contract, "data": "0x",
            "topics": [T_TRANSFER, _topic_addr(frm), _topic_addr(to),
                       _word(token_id)]}


def _erc721_approval(contract, owner, approved, token_id):
    return {"address": contract, "data": "0x",
            "topics": [T_APPROVAL, _topic_addr(owner), _topic_addr(approved),
                       _word(token_id)]}


def _erc20_transfer(contract, frm, to, amount):
    """THREE topics: value rides in data."""
    return {"address": contract, "data": _word(amount),
            "topics": [T_TRANSFER, _topic_addr(frm), _topic_addr(to)]}


def _approval_for_all(contract, owner, operator, approved):
    return {"address": contract, "data": _word(1 if approved else 0),
            "topics": [T_APPROVAL_FOR_ALL, _topic_addr(owner),
                       _topic_addr(operator)]}


def _transfer_single(contract, operator, frm, to, token_id, value):
    """FOUR topics, and the FIRST is the operator, not the sender."""
    return {"address": contract,
            "data": "0x" + _raw_word(token_id) + _raw_word(value),
            "topics": [T_TRANSFER_SINGLE, _topic_addr(operator),
                       _topic_addr(frm), _topic_addr(to)]}


def _transfer_batch(contract, operator, frm, to, ids, values):
    ids_at = 0x40
    values_at = ids_at + 0x20 * (1 + len(ids))
    data = (_raw_word(ids_at) + _raw_word(values_at)
            + _raw_word(len(ids)) + "".join(_raw_word(i) for i in ids)
            + _raw_word(len(values)) + "".join(_raw_word(v) for v in values))
    return {"address": contract, "data": "0x" + data,
            "topics": [T_TRANSFER_BATCH, _topic_addr(operator),
                       _topic_addr(frm), _topic_addr(to)]}


# --- ERC-721 ---------------------------------------------------------------

def test_erc721_transfer_out_is_measured():
    d = _sim(_entry(_erc721_transfer(NFT, HOLDER, OTHER, 42)))
    assert d.ok is True
    assert d.holder_nft_out == ((NFT.lower(), "erc721", OTHER.lower(), 42, 1),)
    assert d.holder_nft_in == ()


def test_erc721_transfer_in_is_measured():
    """The direction the old filter could never see: holder as RECIPIENT."""
    d = _sim(_entry(_erc721_transfer(NFT, OTHER, HOLDER, 42)))
    assert d.ok is True
    assert d.holder_nft_in == ((NFT.lower(), "erc721", OTHER.lower(), 42, 1),)
    assert d.holder_nft_out == ()


def test_an_erc721_transfer_between_third_parties_is_ignored():
    d = _sim(_entry(_erc721_transfer(NFT, OTHER, MARKET, 42)))
    assert d.ok is True
    assert d.holder_nft_out == ()
    assert d.holder_nft_in == ()


def test_an_erc721_token_id_never_lands_in_the_erc20_transfer_tuple():
    """A token id is an IDENTIFIER. Folding it into holder_transfers would run
    the ERC-20 amount arithmetic over it."""
    d = _sim(_entry(_erc721_transfer(NFT, HOLDER, OTHER, 42)))
    assert d.holder_transfers == ()


def test_erc721_single_token_approval_is_measured():
    d = _sim(_entry(_erc721_approval(NFT, HOLDER, MARKET, 42)))
    assert d.ok is True
    assert d.holder_nft_approvals == ((NFT.lower(), MARKET.lower(), 42),)
    assert d.holder_approvals == ()


# --- ERC-1155 --------------------------------------------------------------

def test_erc1155_transfer_single_out_is_measured():
    d = _sim(_entry(_transfer_single(NFT, HOLDER, HOLDER, OTHER, 7, 3)))
    assert d.ok is True
    assert d.holder_nft_out == ((NFT.lower(), "erc1155", OTHER.lower(), 7, 3),)


def test_erc1155_keys_the_sender_off_topic2_not_topic1():
    """⚠️ The operator is topics[1]. A marketplace we approved moves OUR token:
    operator=MARKET, from=HOLDER. Keying off topics[1] would miss this."""
    d = _sim(_entry(_transfer_single(NFT, MARKET, HOLDER, OTHER, 7, 1)))
    assert d.holder_nft_out == ((NFT.lower(), "erc1155", OTHER.lower(), 7, 1),)


def test_erc1155_operator_alone_is_not_our_movement():
    """We operated on someone else's behalf; nothing of ours moved. Keying off
    topics[1] would report this as our own outflow."""
    d = _sim(_entry(_transfer_single(NFT, HOLDER, OTHER, MARKET, 7, 1)))
    assert d.holder_nft_out == ()
    assert d.holder_nft_in == ()


def test_erc1155_transfer_single_in_is_measured():
    d = _sim(_entry(_transfer_single(NFT, MARKET, OTHER, HOLDER, 7, 5)))
    assert d.holder_nft_in == ((NFT.lower(), "erc1155", OTHER.lower(), 7, 5),)


def test_erc1155_transfer_batch_yields_one_entry_per_id():
    d = _sim(_entry(_transfer_batch(NFT, HOLDER, HOLDER, OTHER, [7, 9], [1, 2])))
    assert d.ok is True
    assert d.holder_nft_out == (
        (NFT.lower(), "erc1155", OTHER.lower(), 7, 1),
        (NFT.lower(), "erc1155", OTHER.lower(), 9, 2),
    )


def test_a_transfer_batch_with_mismatched_array_lengths_is_skipped():
    """Malformed, so unreadable — and an unreadable log is skipped, as today."""
    log = _transfer_batch(NFT, HOLDER, HOLDER, OTHER, [7, 9], [1, 2])
    ids_at = 0x40
    values_at = ids_at + 0x20 * 3
    log["data"] = ("0x" + _raw_word(ids_at) + _raw_word(values_at)
                   + _raw_word(2) + _raw_word(7) + _raw_word(9)
                   + _raw_word(1) + _raw_word(1))  # says 1 value, sends 2 ids
    d = _sim(_entry(log))
    assert d.ok is True
    assert d.holder_nft_out == ()


# --- ApprovalForAll — the drain vector -------------------------------------

def test_approval_for_all_grant_is_measured():
    d = _sim(_entry(_approval_for_all(NFT, HOLDER, MARKET, True)))
    assert d.ok is True
    assert d.holder_operator_grants == ((NFT.lower(), MARKET.lower(), True),)


def test_approval_for_all_revoke_is_measured_as_false():
    d = _sim(_entry(_approval_for_all(NFT, HOLDER, MARKET, False)))
    assert d.holder_operator_grants == ((NFT.lower(), MARKET.lower(), False),)


def test_approval_for_all_never_pollutes_the_erc20_allowance_tuple():
    """⚠️ holder_approvals' third element is an AMOUNT. ApprovalForAll's payload
    is a BOOL. Folding them puts True where an int is expected and corrupts the
    existing allowance arithmetic."""
    d = _sim(_entry(_approval_for_all(NFT, HOLDER, MARKET, True)))
    assert d.holder_approvals == ()


def test_another_owners_approval_for_all_is_ignored():
    d = _sim(_entry(_approval_for_all(NFT, OTHER, MARKET, True)))
    assert d.holder_operator_grants == ()


# --- the ERC-20 path must be untouched -------------------------------------

def test_erc20_transfer_is_unchanged_by_the_nft_branch():
    d = _sim(_entry(_erc20_transfer(USDC, HOLDER, OTHER, 777)))
    assert d.holder_transfers == ((USDC.lower(), OTHER.lower(), 777),)
    assert d.holder_nft_out == ()
    assert d.holder_nft_in == ()


def test_a_mixed_log_set_routes_each_event_to_its_own_field():
    d = _sim(_entry(
        _erc20_transfer(USDC, HOLDER, OTHER, 777),
        _erc721_transfer(NFT, HOLDER, OTHER, 42),
        _approval_for_all(NFT, HOLDER, MARKET, True),
        _transfer_single(NFT, MARKET, OTHER, HOLDER, 7, 2),
    ))
    assert d.holder_transfers == ((USDC.lower(), OTHER.lower(), 777),)
    assert d.holder_nft_out == ((NFT.lower(), "erc721", OTHER.lower(), 42, 1),)
    assert d.holder_nft_in == ((NFT.lower(), "erc1155", OTHER.lower(), 7, 2),)
    assert d.holder_operator_grants == ((NFT.lower(), MARKET.lower(), True),)
