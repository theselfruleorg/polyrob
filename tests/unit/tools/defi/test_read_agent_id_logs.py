"""``read_agent_id`` on a NON-enumerable ERC-8004 Identity Registry (testnet-run 2026-09-29 F3).

Measured: ``supportsInterface(0x780e9d63)`` is false at the pinned registries on 4663 and 46630,
so ``tokenOfOwnerByIndex`` REVERTS; ``_rpc`` raised it and every registered wallet read as
"could not check". The id now comes from the registry's ``Transfer(_, holder, id)`` logs,
confirmed with ``ownerOf``, and the read RAISES when they do not account for ``balanceOf``.
"""
import pytest

from core.wallet import erc8004
from core.wallet.abi import encode_call
from core.wallet.simulation import _TOPIC_TRANSFER
from tools.defi import agent_registration as ar

HOLDER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
REG = erc8004.resolve_identity_registry("robinhood")
SEL_BALANCE = encode_call("balanceOf", [{"type": "address"}], [HOLDER])[:10]
SEL_OWNER_OF = encode_call("ownerOf", [{"type": "uint256"}], [0])[:10]
SEL_ENUM = encode_call("tokenOfOwnerByIndex", [{"type": "address"}, {"type": "uint256"}], [HOLDER, 0])[:10]


def _u(n):
    return "0x" + format(int(n), "064x")


def _a(addr):
    return "0x" + addr.lower()[2:].rjust(64, "0")


class Chain:
    """A fake registry: balances, owners, Transfer logs, enumerable or not."""

    def __init__(self, *, owners, transfers, enumerable=False, head=80_000_000,
                 supports_raises=False, max_span=None):
        self.owners = {int(k): v.lower() for k, v in owners.items()}
        self.transfers = transfers          # [(block, from, to, id)]
        self.enumerable = enumerable
        self.head = head
        self.supports_raises = supports_raises
        self.max_span = max_span
        self.windows = []

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            if self.max_span and hi - lo + 1 > self.max_span:
                raise RuntimeError("query spans too many blocks")
            self.windows.append((lo, hi))
            want_to = q["topics"][2]
            return [{"address": REG.lower(), "blockNumber": hex(b),
                     "topics": [_TOPIC_TRANSFER, _a(f), _a(t), _u(i)]}
                    for (b, f, t, i) in self.transfers
                    if lo <= b <= hi and _a(t) == want_to]
        assert method == "eth_call"
        data = params[0]["data"]
        if data.startswith(SEL_BALANCE):
            who = "0x" + data[-40:]
            return _u(sum(1 for o in self.owners.values() if o == who.lower()))
        if data.startswith("0x01ffc9a7"):
            if self.supports_raises:
                raise RuntimeError("execution reverted")
            return _u(1 if self.enumerable else 0)
        if data.startswith(SEL_ENUM):
            if not self.enumerable:
                raise RuntimeError("RpcError eth_call: execution reverted")
            return _u(min(i for i, o in self.owners.items() if o == HOLDER.lower()))
        if data.startswith(SEL_OWNER_OF):
            i = int(data[10:], 16)
            if i not in self.owners:
                raise RuntimeError("execution reverted: ERC721NonexistentToken")
            return _a(self.owners[i])
        raise AssertionError(f"unexpected call {data[:10]}")


def test_an_unregistered_wallet_reads_none_without_a_scan():
    chain = Chain(owners={0: OTHER}, transfers=[(40_000_000, "0x" + "0" * 40, OTHER, 0)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) is None
    assert chain.windows == []


def test_a_non_enumerable_registry_names_the_id_from_transfer_logs():
    chain = Chain(owners={0: OTHER, 7: HOLDER},
                  transfers=[(40_000_000, "0x" + "0" * 40, OTHER, 0),
                             (50_000_000, "0x" + "0" * 40, HOLDER, 7)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 7


def test_agent_id_zero_is_a_real_id():
    chain = Chain(owners={0: HOLDER}, transfers=[(34_617_892, "0x" + "0" * 40, HOLDER, 0)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 0


def test_a_token_transferred_away_is_not_counted():
    chain = Chain(owners={3: OTHER, 9: HOLDER},
                  transfers=[(40_000_000, "0x" + "0" * 40, HOLDER, 3),
                             (41_000_000, HOLDER, OTHER, 3),
                             (60_000_000, "0x" + "0" * 40, HOLDER, 9)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 9


def test_logs_that_do_not_account_for_the_balance_raise():
    # the balance says 1, the logs from the scan start show nothing (e.g. a wrong start)
    chain = Chain(owners={5: HOLDER}, transfers=[])
    with pytest.raises(RuntimeError, match="refusing to guess"):
        ar.read_agent_id(chain, chain="robinhood", holder=HOLDER)


def test_the_scan_starts_at_the_measured_block_and_stays_within_the_node_limit():
    chain = Chain(owners={2: HOLDER}, transfers=[(70_000_000, "0x" + "0" * 40, HOLDER, 2)],
                  max_span=10_000_000)
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 2
    assert chain.windows[0][0] == erc8004.identity_logs_from("robinhood") == 34_617_892
    assert chain.windows[-1][1] == chain.head
    assert all(hi - lo + 1 <= ar.LOG_SCAN_STEP for lo, hi in chain.windows)
    # contiguous: no block is skipped between windows
    assert all(b[0] == a[1] + 1 for a, b in zip(chain.windows, chain.windows[1:]))


def test_an_explicit_from_block_overrides_the_row():
    chain = Chain(owners={2: HOLDER}, transfers=[(100, "0x" + "0" * 40, HOLDER, 2)], head=1_000)
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER, from_block=0) == 2


def test_a_failed_log_read_raises_never_none():
    chain = Chain(owners={2: HOLDER}, transfers=[], max_span=1)
    with pytest.raises(RuntimeError):
        ar.read_agent_id(chain, chain="robinhood", holder=HOLDER)


def test_an_enumerable_registry_still_verifies_mint_provenance():
    chain = Chain(owners={4: HOLDER}, transfers=[(40_000_000, "0x" + "0" * 40, HOLDER, 4)], enumerable=True)
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 4
    assert chain.windows


def test_a_reverting_supports_interface_falls_back_to_the_logs():
    chain = Chain(owners={4: HOLDER}, transfers=[(40_000_000, "0x" + "0" * 40, HOLDER, 4)],
                  supports_raises=True)
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 4


def test_the_testnet_row_carries_its_own_measured_start():
    assert erc8004.identity_logs_from("robinhood-testnet") == 99_678_682
    assert erc8004.identity_logs_from("base") == 0


def test_the_register_verb_caller_refuses_a_registered_wallet_by_its_id(monkeypatch):
    """The one core caller: ``DefiTradeTool._read_agent_id`` (register_agent's double-mint
    check). Before F3 it raised RpcError here and the verb said "could not check"."""
    from core.wallet import onchain
    from tools.defi.trade_tool import DefiTradeTool

    chain = Chain(owners={11: HOLDER}, transfers=[(40_000_000, "0x" + "0" * 40, HOLDER, 11)])
    monkeypatch.setattr(onchain, "rpc_url_for_chain", lambda c: "http://fake")
    monkeypatch.setattr(onchain, "_rpc", lambda url, m, a, t=8.0: chain(m, a))
    got = DefiTradeTool._read_agent_id(ar, "robinhood", HOLDER)
    assert got == 11
    why = ar.check_not_already_registered(existing_agent_id=got, chain="robinhood")
    assert why and "agentId 11" in why


def test_agent_id_zero_refuses_a_second_register():
    why = ar.check_not_already_registered(existing_agent_id=0, chain="robinhood")
    assert why and "agentId 0" in why
    assert ar.check_not_already_registered(existing_agent_id=None, chain="robinhood") is None


def test_unsolicited_identity_does_not_block_registration():
    chain = Chain(owners={1: HOLDER}, transfers=[(40_000_000, OTHER, HOLDER, 1)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) is None


def test_selects_self_minted_identity_even_with_a_lower_unsolicited_id():
    chain = Chain(owners={1: HOLDER, 9: HOLDER}, transfers=[
        (40_000_000, OTHER, HOLDER, 1), (40_000_001, "0x" + "0" * 40, HOLDER, 9)])
    assert ar.read_agent_id(chain, chain="robinhood", holder=HOLDER) == 9


def test_multiple_self_minted_identities_refuse_instead_of_guessing():
    chain = Chain(owners={1: HOLDER, 9: HOLDER}, transfers=[
        (40_000_000, "0x" + "0" * 40, HOLDER, i) for i in (1, 9)])
    with pytest.raises(RuntimeError, match="multiple self-minted"):
        ar.read_agent_id(chain, chain="robinhood", holder=HOLDER)
