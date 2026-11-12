"""The guard pre-flight for a PolyrobAccount (polyrob-desk SPEC §7.1): the collection's own account.

Before this, core verified only Tokenbound AccountV3, so every call through a POLYROB's account
failed closed in the pre-flight: the implementation was not AccountV3, and ``isLocked`` /
``isTrustedForwarder`` revert on a PolyrobAccount (it has neither, and no fallback). Now:

* the implementation must be the one pinned by the account's collection profile
  (``transfer_safe``), and its live code must hash to ``account_impl_runtime_sha256`` (G-3);
* the lock and the forwarder reads are skipped for it (they would revert);
* its ``JOURNAL()`` must be the profile's ``journal_log``;
* AccountV3 behaves exactly as before (``test_tx_guard_via_account.py``).
"""
import hashlib

import pytest

from core.wallet import account_kinds, abi, collection_registry, erc6551, simulation
from tests.unit.core.wallet.test_tx_guard_via_account import (
    BASE_ID, COLLECTION, OWNER, STATE, TOKEN_ID, TREASURY, _erc20_deltas, _erc20_inner,
    _erc20_intent, _outer, _run, _via)

IMPL = "0x" + "a1" * 20
IMPL_CODE = "0x" + "a1a2" * 40
JOURNAL = "0x" + "ee" * 20
ACCOUNT = erc6551.account_address(BASE_ID, COLLECTION, TOKEN_ID, implementation=IMPL)
_H00 = hashlib.sha256(b"\x00").hexdigest()


def _word(n):
    return "0x" + int(n).to_bytes(32, "big").hex()


class PolyrobRpc:
    """A PolyrobAccount clone of the pinned collection's token #1. ``isLocked``,
    ``lockedUntil`` and ``isTrustedForwarder`` REVERT, as on chain."""

    def __init__(self, *, owner=TREASURY, state=STATE, impl_code=IMPL_CODE, journal=JOURNAL,
                 ready=False):
        self.owner, self.state, self.impl_code, self.journal = owner, state, impl_code, journal
        self.ready = ready   # saleReadyOf(id): False, True, or None (the read reverts)
        self.reads = []

    def __call__(self, method, params):
        if method == "eth_getCode":
            addr = params[0].lower()
            if addr == ACCOUNT.lower():
                return ("0x363d3d373d3d3d363d73" + IMPL[2:] + "5af43d82803e903d91602b57fd5bf3"
                        + "00" * 128)
            if addr == IMPL:
                return self.impl_code
            return "0x00"  # the registry and the collection (pinned as sha256(b"\x00"))
        if method == "eth_call":
            sel = params[0]["data"][:10]
            self.reads.append(sel)
            answers = {
                abi.selector("owner()"): _word(int(self.owner, 16)),
                abi.selector("state()"): _word(self.state),
                abi.selector("JOURNAL()"): _word(int(self.journal, 16)),
                abi.selector("token()"): "0x" + abi.encode(
                    [{"type": "uint256"}, {"type": "address"}, {"type": "uint256"}],
                    [BASE_ID, COLLECTION, TOKEN_ID]).hex(),
            }
            if self.ready is not None:   # 115 W7: the sale hold reads the collection's view
                answers[abi.selector("saleReadyOf(uint256)")] = _word(int(bool(self.ready)))
            if sel not in answers:
                raise RuntimeError("execution reverted")  # no lock, no forwarder, no fallback
            return answers[sel]
        raise AssertionError(method)


def _profile(*, impl_sha=None, journal_log=JOURNAL):
    std = collection_registry.Erc6551pStandard(
        market="0x" + "b1" * 20, market_runtime_sha256=_H00, zone="0x" + "c1" * 20,
        zone_runtime_sha256=_H00)
    sha = impl_sha or hashlib.sha256(bytes.fromhex(IMPL_CODE[2:])).hexdigest()
    return collection_registry.CollectionProfile(
        spec=collection_registry.SPEC_V2, capabilities=(), chain_id=BASE_ID, address=COLLECTION.lower(),
        runtime_sha256=_H00, deploy_block=0, max_supply=3, journal_log=journal_log, erc6551p=std,
        accounts=(collection_registry.AccountVersion(
            erc6551.REGISTRY.lower(), IMPL, 0, account_kinds.ERC6551P, sha),))


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): _H00,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    monkeypatch.setattr(collection_registry, "profiles", lambda: (_profile(),))


def _go(rpc, **kw):
    return _run(_via(_erc20_intent(), account=ACCOUNT, **kw), _erc20_deltas(),
                _outer(_erc20_inner(), to=ACCOUNT), holder=TREASURY, rpc=rpc)


def test_an_owned_polyrob_account_passes_the_preflight_without_lock_or_forwarder_reads():
    rpc = PolyrobRpc()
    d = _go(rpc)
    assert d.allowed is True, d.reason
    assert abi.selector("isLocked()") not in rpc.reads
    assert abi.selector("isTrustedForwarder(address)") not in rpc.reads
    assert abi.selector("JOURNAL()") in rpc.reads


def test_account_v3_is_no_longer_required_even_where_it_is_absent(monkeypatch):
    """The AccountV3 pin is not read for a PolyrobAccount: a chain without AccountV3 works."""
    d = _go(PolyrobRpc())
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("rpc, needle", [
    (PolyrobRpc(impl_code="0x" + "bb" * 40), "un-reviewed"),
    (PolyrobRpc(impl_code="0x"), "NO code"),
    (PolyrobRpc(owner=OWNER), "does not own"),
    (PolyrobRpc(state=STATE + 1), "state moved"),
    (PolyrobRpc(journal="0x" + "ef" * 20), "names the JournalLog"),
])
def test_polyrob_preflight_refusals(rpc, needle):
    d = _go(rpc)
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_profile_without_a_journal_log_skips_the_journal_read(monkeypatch):
    monkeypatch.setattr(collection_registry, "profiles", lambda: (_profile(journal_log=None),))
    rpc = PolyrobRpc(journal="0x" + "ef" * 20)
    assert _go(rpc).allowed is True
    assert abi.selector("JOURNAL()") not in rpc.reads


def test_an_account_v3_profile_does_not_accept_a_polyrob_clone(monkeypatch):
    v3 = collection_registry.CollectionProfile(
        spec=collection_registry.SPEC_V2, capabilities=(), chain_id=BASE_ID, address=COLLECTION.lower(),
        runtime_sha256=_H00, deploy_block=0, max_supply=3,
        accounts=(collection_registry.AccountVersion(erc6551.REGISTRY.lower(),
                                                     erc6551.ACCOUNT_V3_IMPL.lower(), 0),))
    monkeypatch.setattr(collection_registry, "profiles", lambda: (v3,))
    d = _go(PolyrobRpc())
    assert d.allowed is False and "is not the pinned ERC-6551 account" in d.reason, d.reason


def test_a_reverted_simulation_carries_its_revert_data_for_the_guard_to_name():
    """WL3: the simulation names nothing itself; it hands the revert data to the guard, which
    reads it only against the tables of the contracts the call reaches."""
    entry = {"status": "0x0", "error": {"message": "execution reverted",
                                        "data": "0xd88c09e3" + "00" * 96}}
    assert simulation._revert_data(entry) == "0xd88c09e3" + "00" * 96
    assert simulation._revert_data({"status": "0x0", "returnData": "0xe4073933" + "00" * 64}) \
        == "0xe4073933" + "00" * 64
    assert simulation._revert_data({"status": "0x0", "error": "out of gas"}) is None


def test_every_named_error_is_the_selector_of_its_signature():
    from core.wallet import custom_errors
    sigs = ["NotOwner()", "Reentered()", "SelfCall()", "OperationNotSupported()", "ReadyNotLast()",
            "Refused(address,bytes4)", "BadEncoding()", "TooManyGrants()", "ReadGas()",
            "ReadFailed(address)", "GasReserve()", "OwnerChanged()",
            "StandingGrant(uint256,address,address)", "OwnershipCycle()"]
    want = {abi.selector(s): s for s in sigs}
    assert {k: custom_errors.canonical(v[0]) for k, v in account_kinds.ERC6551P_ERRORS.items()} == want


# ------------------------------------------------------------------ N5 / WL3: revert names
def test_a_revert_through_the_erc6551p_account_is_named_with_its_arguments():
    """The guard names the account's own error, decoded with its arguments."""
    target, sel = "0x" + "77" * 20, "0x095ea7b3"
    data = "0xe4073933" + "00" * 12 + target[2:] + sel[2:] + "00" * 28
    d = _run(_via(_erc20_intent(), account=ACCOUNT), simulation.Deltas(
            ok=False, error="the transaction REVERTS in simulation (execution reverted)",
            revert_data=data), _outer(_erc20_inner(), to=ACCOUNT), holder=TREASURY,
        rpc=PolyrobRpc())
    assert d.allowed is False
    assert (f"the ERC-6551P account refused it on chain: Refused(target={target}, "
            f"selector={sel})") in d.reason, d.reason


def test_a_collection_error_through_the_account_is_named():
    d = _run(_via(_erc20_intent(), account=ACCOUNT), simulation.Deltas(
        ok=False, error="the transaction REVERTS in simulation (execution reverted)",
        revert_data="0xd15af9b5"), _outer(_erc20_inner(), to=ACCOUNT), holder=TREASURY,
        rpc=PolyrobRpc())
    assert "the pinned collection refused it on chain: AccountInBatch()" in d.reason, d.reason
