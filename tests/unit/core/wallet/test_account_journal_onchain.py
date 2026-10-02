"""J1/J3/J4 (polyrob-desk handoff-core) — the account journal on chain, its chain, its file lock.

* ``core.wallet.journal_log``: the ``log(bytes)`` codec and the ``Entry`` reader (literals pinned
  to their signatures; an undecodable entry is skipped, a failed read raises).
* J3: the next entry extends the account's ON-CHAIN chain — a sold account continues from its
  seller's last entry; an unreadable chain refuses rather than starting a new one.
* J4: two writers of one journal never both append the same ``seq``.
"""
import threading

import pytest
from eth_account import Account

from core.wallet import journal_log, nft_account
from core.wallet.nft_account import HeldNft
from core.wallet.signer import LocalEoaSigner

ACCOUNT = "0xe0f47C083B28C76124129786cBd02a4489b86ec4"
JLOG = "0x10910910910910910910910910910910910910a1"
COLL = "0x6666666666666666666666666666666666666666"


def _key():
    return LocalEoaSigner(bytes(Account.create().key))


def _held(journal=JLOG):
    return HeldNft(chain="robinhood", chain_id=4663, collection=COLL, token_id=1, account=ACCOUNT,
                   journal_log=journal, deploy_block=100)


def _signed(prior, key, kind="tend", text="t"):
    return nft_account.sign_entry(nft_account.build_entry(
        prior=prior, account=ACCOUNT, chain_id=4663, kind=kind, text=text, owner=key.address), key)


def _log(entry, block, index=0, address=JLOG, raw=None):
    data = journal_log.encode_log(raw if raw is not None else nft_account.canonical(entry))
    return {"address": address, "blockNumber": hex(block), "logIndex": hex(index),
            "topics": [journal_log.TOPIC_ENTRY, "0x" + "0" * 24 + ACCOUNT.lower()[2:]],
            "data": "0x" + data[len(journal_log.LOG_SELECTOR):]}


class LogRpc:
    def __init__(self, logs=(), broken=False, head=0x200):
        self.logs, self.broken, self.head, self.queries = list(logs), broken, head, []

    def __call__(self, method, params):
        if self.broken:
            raise RuntimeError("rpc down")
        if method == "eth_blockNumber":
            return hex(self.head)
        assert method == "eth_getLogs", method
        self.queries.append(params[0])
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]


def _kw(tmp_path):
    return dict(home_dir=tmp_path, user_id="u", instance_id="i")


# --- the codec ------------------------------------------------------------------------------

def test_the_literals_are_their_signatures():
    from eth_utils import keccak
    assert journal_log.LOG_SELECTOR == "0x" + keccak(text="log(bytes)").hex()[:8]
    assert journal_log.TOPIC_ENTRY == "0x" + keccak(text="Entry(address,bytes)").hex()


@pytest.mark.parametrize("n", [0, 1, 31, 32, 33, 300])
def test_log_calldata_round_trips(n):
    raw = bytes(range(256)) * 2
    raw = raw[:n]
    assert journal_log.decode_log(journal_log.encode_log(raw)) == raw


def test_the_calldata_is_the_abi_encoding():
    from core.wallet import abi
    raw = b'{"a":1}'
    assert journal_log.encode_log(raw) == abi.encode_call("log", [{"type": "bytes"}], [raw])


@pytest.mark.parametrize("data", [
    "0xdeadbeef" + "00" * 64,                                  # another selector
    journal_log.encode_log(b"abc") + "00" * 32,                # an extra word
    journal_log.encode_log(b"abc")[:-2] + "01",                # dirty padding
    journal_log.LOG_SELECTOR + "00" * 31 + "40" + "00" * 32,   # wrong offset
])
def test_non_canonical_calldata_is_refused(data):
    with pytest.raises(journal_log.JournalLogError):
        journal_log.decode_log(data)


def test_an_entry_is_exactly_the_journal_fields_in_canonical_form():
    e = _signed([], _key())
    assert journal_log.parse_entry(nft_account.canonical(e)) == e
    with pytest.raises(journal_log.JournalLogError):
        journal_log.parse_entry(nft_account.canonical(dict(e, extra=1)))
    with pytest.raises(journal_log.JournalLogError):
        journal_log.parse_entry(nft_account.canonical(e) + b" ")
    with pytest.raises(journal_log.JournalLogError):
        journal_log.parse_entry(b"\xff")


def test_the_reader_orders_by_block_and_skips_garbage():
    k = _key()
    e0 = _signed([], k)
    e1 = _signed([e0], k)
    rpc = LogRpc([_log(e1, 0x150, 2), _log(None, 0x150, 1, raw=b"junk"),
                  _log(e0, 0x120), _log(e0, 0x130, address="0x" + "99" * 20)])
    got = journal_log.read_entries(rpc, JLOG, ACCOUNT, 0x100, step=0x40)
    assert got == [e0, e1]
    assert all(q["topics"][1].endswith(ACCOUNT.lower()[2:]) for q in rpc.queries)
    assert len(rpc.queries) > 1                                   # chunked


def test_an_unreadable_log_is_never_no_entries():
    with pytest.raises(RuntimeError):
        journal_log.read_entries(LogRpc(broken=True), JLOG, ACCOUNT, 0)


# --- J3: the chain continues from the chain -------------------------------------------------

def test_a_sold_account_continues_from_the_sellers_last_entry(tmp_path):
    seller, buyer = _key(), _key()
    e0 = _signed([], seller)
    e1 = _signed([e0], seller, kind="handover", text="sold")
    rpc = LogRpc([_log(e0, 0x110), _log(e1, 0x120)])
    entry = nft_account.prepare_entry(_held(), buyer, kind="note", text="hello", rpc=rpc,
                                      **_kw(tmp_path))
    assert entry["seq"] == 2 and entry["prev"] == nft_account.digest(e1)
    assert entry["owner"] == buyer.address.lower()
    assert nft_account.recover_owner(entry) == buyer.address.lower()


def test_an_unreadable_chain_refuses_a_new_one(tmp_path):
    from tools.defi import account_mode
    with pytest.raises(RuntimeError):
        nft_account.prepare_entry(_held(), _key(), kind="note", text="x", rpc=LogRpc(broken=True),
                                  **_kw(tmp_path))
    entry, why = account_mode.prepare_journal(_held(), _key(), kind="note", text="x",
                                              rpc=LogRpc(broken=True))
    assert entry is None and "rpc down" in why


def test_a_forged_or_broken_entry_on_chain_is_ignored():
    k, stranger = _key(), _key()
    e0 = _signed([], k)
    forged = dict(_signed([e0], k), text="edited")             # signature no longer covers it
    gap = _signed([e0, _signed([e0], k)], k)                    # seq 2 with no seq 1 on chain
    chain = nft_account.verified_chain([e0, forged, gap], account=ACCOUNT, chain_id=4663)
    assert chain == [e0]
    other = nft_account.sign_entry(dict(nft_account.build_entry(
        prior=[e0], account=ACCOUNT, chain_id=4663, kind="note", text="x",
        owner=stranger.address)), stranger)
    assert nft_account.verified_chain([e0, other], account=ACCOUNT, chain_id=4663) == [e0, other]


def test_an_in_flight_local_entry_extends_the_chain(tmp_path):
    k = _key()
    e0 = _signed([], k)
    e1 = _signed([e0], k)                       # sent, not yet mined: in the cache only
    held = _held()
    nft_account.cache_entry(held, e1, rpc=None, **_kw(tmp_path))
    rpc = LogRpc([_log(e0, 0x110)])
    nxt = nft_account.prepare_entry(held, k, kind="note", text="n", rpc=rpc, **_kw(tmp_path))
    assert nxt["seq"] == 2 and nxt["prev"] == nft_account.digest(e1)


def test_a_stale_local_chain_loses_to_the_chain(tmp_path):
    """The local file says seq 0 was X; the chain says seq 0 is Y (another owner wrote it)."""
    me, them = _key(), _key()
    mine = _signed([], me, text="local only")
    theirs = _signed([], them, text="on chain")
    held = _held()
    nft_account.cache_entry(held, mine, **_kw(tmp_path))
    rpc = LogRpc([_log(theirs, 0x110)])
    nxt = nft_account.prepare_entry(held, me, kind="note", text="n", rpc=rpc, **_kw(tmp_path))
    assert nxt["seq"] == 1 and nxt["prev"] == nft_account.digest(theirs)


def test_without_a_journal_log_the_local_file_is_the_chain(tmp_path):
    k = _key()
    held = _held(journal=None)
    e0, _ = nft_account.append_journal(held, k, kind="note", text="a", **_kw(tmp_path))
    assert nft_account.prior_entries(held, **_kw(tmp_path)) == [e0]
    with pytest.raises(ValueError):
        nft_account.prepare_entry(held, k, kind="note", text="x", rpc=LogRpc(), **_kw(tmp_path))


def test_the_cache_keeps_the_chain_and_the_entry(tmp_path):
    k = _key()
    e0 = _signed([], k)
    e1 = _signed([e0], k)
    held = _held()
    path = nft_account.cache_entry(held, e1, rpc=LogRpc([_log(e0, 0x110)]), **_kw(tmp_path))
    assert nft_account.load_journal(path) == [e0, e1]


# --- J4: the file lock ----------------------------------------------------------------------

def test_concurrent_appends_never_share_a_seq(tmp_path):
    k = _key()
    held = _held(journal=None)
    errors = []

    def write(i):
        try:
            nft_account.append_journal(held, k, kind="note", text=f"w{i}", **_kw(tmp_path))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=write, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    entries = nft_account.prior_entries(held, **_kw(tmp_path))
    assert [e["seq"] for e in entries] == list(range(12))
    assert nft_account.verified_chain(entries, account=ACCOUNT, chain_id=4663) == entries
