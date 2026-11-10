"""The account view and the approval lines for a PolyrobAccount (the POLYROB collection's own account).

A PolyrobAccount has no lock (``isLocked`` / ``lockedUntil`` revert), signs nothing and refuses
standing approvals. The view must not read the lock (an unread lock would read as "unknown"
on every POLYROB), must name the account kind, must show the collection's ``saleReadyOf``, and
must say what "no approvals" means for it (the account refuses them; R-1/R-5/R-6 stay outside).
"""
import hashlib
import os

import pytest

from core.wallet import abi, erc6551, erc8004
from core.wallet.signer import LocalEoaSigner
from tests.collection_pins import CODE, RUNTIME_SHA256, pin, polyrob_profile
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import RevokeAllParams, SnapshotParams

PINNED = "0xC011000000000000000000000000000000000001"
IMPL = "0x" + "a1" * 20
ACCOUNT = erc6551.account_address(4663, PINNED, 3, implementation=IMPL)
REG_CODE = "0x" + "11" * 10


def _w(v):
    return "0x" + "0" * 24 + v[2:].lower() if isinstance(v, str) else "0x" + f"{int(v):064x}"


def _clone():
    footer = (0).to_bytes(32, "big") + (4663).to_bytes(32, "big") + b"\x00" * 12 \
        + bytes.fromhex(PINNED[2:]) + (3).to_bytes(32, "big")
    return "0x363d3d373d3d3d363d73" + IMPL[2:] + "5af43d82803e903d91602b57fd5bf3" + footer.hex()


class PolyrobChain:
    def __init__(self, owner, *, ready=False):
        self.owner, self.ready = owner, ready
        self.reads = []

    def __call__(self, method, params, *a, **k):
        if method == "eth_blockNumber":
            return hex(0x300)
        if method == "eth_getCode":
            a_ = params[0].lower()
            if a_ in (PINNED.lower(), IMPL):
                return CODE
            if a_ == ACCOUNT.lower():
                return _clone()
            return REG_CODE
        if method == "eth_getLogs":
            return []
        if method == "eth_getBalance":
            return hex(5 * 10 ** 15)
        if method == "eth_call":
            sel = params[0]["data"][:10]
            self.reads.append(sel)
            names = {abi.selector(s): s for s in ("ownerOf(uint256)", "state()", "balanceOf(address)",
                                                  "saleReadyOf(uint256)")}
            name = names.get(sel)
            if name == "ownerOf(uint256)":
                return _w(self.owner)
            if name == "state()":
                return _w(11)
            if name == "balanceOf(address)":
                return _w(0)
            if name == "saleReadyOf(uint256)":
                return _w(int(self.ready))
            raise RuntimeError("execution reverted")  # isLocked, lockedUntil: no such function
        raise AssertionError(f"{method} {params}")


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, polyrob_profile(PINNED, implementation=IMPL, journal_prefix="POLYROB"))
    h = hashlib.sha256(bytes.fromhex(REG_CODE[2:])).hexdigest()
    # AccountV3's pin is deliberately wrong: a PolyrobAccount view must not depend on it.
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): h,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    FakeRail.sent = []
    assert RUNTIME_SHA256 == hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()
    return LocalEoaSigner(os.urandom(32))


def _no_package(tool):
    tool._impl_override = None
    tool._impl = lambda: None
    return tool


def test_the_snapshot_of_a_polyrob_account_reads_no_lock_and_names_the_kind(armed):
    chain = PolyrobChain(armed.address)
    tool, _ = _tool(armed, chain)
    res = _run(_no_package(tool).agent_nft_snapshot(SnapshotParams(chain="robinhood", nft="3")))
    assert res.error is None, res.error
    text = res.extracted_content
    assert abi.selector("isLocked()") not in chain.reads
    assert abi.selector("lockedUntil()") not in chain.reads
    assert "clone:     PolyrobAccount (pinned)" in text
    assert "locked:" not in text and "lockedUntil" not in text
    assert "sale ready: False" in text and "[note   ] sale ready: not ready" in text
    assert erc6551.POLYROB_NO_APPROVALS in text and "R-1" in text
    assert "signs:     nothing" in text
    assert "unreadable" not in text, text


def test_a_ready_account_says_a_plain_fill_may_move_it(armed):
    tool, _ = _tool(armed, PolyrobChain(armed.address, ready=True))
    text = _run(_no_package(tool).agent_nft_snapshot(SnapshotParams(chain="robinhood", nft="3"))).extracted_content
    assert "sale ready: True" in text and "a plain marketplace fill may move" in text


def test_revoke_all_on_a_polyrob_account_says_why_nothing_is_open(armed):
    tool, _ = _tool(armed, PolyrobChain(armed.address))
    res = _run(tool.agent_nft_revoke_all(RevokeAllParams(chain="robinhood", nft="3")))
    assert res.error is None, res.error
    assert "Nothing to revoke" in res.extracted_content
    assert erc6551.POLYROB_NO_APPROVALS in res.extracted_content


def test_the_view_does_not_depend_on_the_account_v3_pin(armed):
    """verify(rpc, version): the PolyrobAccount version checks the registry + its own hash."""
    from tools.agent_nft import view
    from core.wallet import collection_registry
    (p,) = collection_registry.profiles()
    v = view.build_view(PolyrobChain(armed.address), "robinhood", p, 3, treasury=armed.address)
    assert v.is_polyrob and v.implementation_ok is True and v.locked is None
