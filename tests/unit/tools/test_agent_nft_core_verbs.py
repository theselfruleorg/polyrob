"""C20 (polyrob-desk handoff-core) — snapshot / inspect / journal / bind are core-owned.

They run with NO agent-NFT package. The view reads everything at ONE block, lists open approvals
first with their coverage, names an unreadable value ``unreadable`` (never 0), and appends a
collection's own lines from the package hook ``verbs.extend_view(view, rpc)`` when it exists.
"""
import hashlib
import os
import types

import pytest

from core.wallet import abi, erc6551, erc8004
from core.wallet.signer import LocalEoaSigner
from tests.collection_pins import CODE, pin, profile
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import BindParams, InspectParams, JournalParams, SnapshotParams

PINNED = "0xC011000000000000000000000000000000000001"
ACCOUNT = erc6551.account_address(4663, PINNED, 3)
OTHER = "0x" + "77" * 20
REG_CODE = "0x" + "11" * 10
REGISTRY = erc8004.resolve_identity_registry("robinhood")


def _w(v):
    return "0x" + "0" * 24 + v[2:].lower() if isinstance(v, str) else "0x" + f"{int(v):064x}"


def _clone():
    footer = (0).to_bytes(32, "big") + (4663).to_bytes(32, "big") + b"\x00" * 12 \
        + bytes.fromhex(PINNED[2:]) + (3).to_bytes(32, "big")
    return ("0x363d3d373d3d3d363d73" + erc6551.ACCOUNT_V3_IMPL[2:].lower()
            + "5af43d82803e903d91602b57fd5bf3" + footer.hex())


class ViewChain:
    def __init__(self, owner, *, identities=0, locked_broken=False):
        self.owner, self.identities, self.locked_broken = owner, identities, locked_broken
        self.blocks = set()

    def __call__(self, method, params, *a, **k):
        if params and isinstance(params[-1], str) and params[-1].startswith("0x") and method != "eth_getLogs":
            self.blocks.add(params[-1])
        if method == "eth_blockNumber":
            return hex(0x300)
        if method == "eth_getCode":
            a_ = params[0].lower()
            if a_ == PINNED.lower():
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
            sels = {abi.selector(s): s for s in ("ownerOf(uint256)", "state()", "isLocked()",
                                                 "lockedUntil()", "balanceOf(address)")}
            name = sels.get(sel)
            if name == "ownerOf(uint256)":
                return _w(self.owner)
            if name == "state()":
                return _w(11)
            if name == "isLocked()":
                if self.locked_broken:
                    raise RuntimeError("isLocked reverted")
                return _w(0)
            if name == "lockedUntil()":
                return _w(0)
            if name == "balanceOf(address)":
                return _w(self.identities)
        raise AssertionError(f"{method} {params}")


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    h = hashlib.sha256(bytes.fromhex(REG_CODE[2:])).hexdigest()
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): h,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): h})
    FakeRail.sent = []
    return LocalEoaSigner(os.urandom(32))


def _no_package(tool):
    tool._impl_override = None
    tool._impl = lambda: None
    return tool


def test_snapshot_needs_no_package_and_reads_one_block(armed):
    chain = ViewChain(armed.address)
    tool, seen = _tool(armed, chain)
    res = _run(_no_package(tool).agent_nft_snapshot(SnapshotParams(nft="3")))
    assert res.error is None, res.error
    text = res.extracted_content
    assert text.index("open approvals") < text.index("CHECKS") < text.index("owner:")
    assert "[ok     ] treasury owns the NFT" in text and "journal:   0 entries" in text
    assert chain.blocks == {hex(0x300)}                    # every pinned read at ONE block
    assert seen == []


def test_an_unreadable_value_is_named_never_zero(armed):
    tool, _ = _tool(armed, ViewChain(armed.address, locked_broken=True))
    text = _run(_no_package(tool).agent_nft_snapshot(SnapshotParams(nft="3"))).extracted_content
    assert "locked:    unreadable" in text and "[unknown] locked" in text
    assert "isLocked reverted" in text


def test_inspect_any_pinned_nft_and_the_package_lines(armed):
    tool, _ = _tool(armed, ViewChain(OTHER))
    tool._impl_override = types.SimpleNamespace(
        extend_view=lambda view, rpc: [f"  face:      pending (#{view.token_id})"])
    res = _run(tool.agent_nft_inspect(InspectParams(target=f"robinhood:{PINNED}/3")))
    assert res.error is None, res.error
    assert f"owner:     {erc6551._checksum(bytes.fromhex(OTHER[2:]))}" in res.extracted_content
    assert res.extracted_content.rstrip().endswith("face:      pending (#3)")


def test_a_failing_package_hook_is_named_not_fatal(armed):
    tool, _ = _tool(armed, ViewChain(OTHER))

    def boom(view, rpc):
        raise RuntimeError("bad face")
    tool._impl_override = types.SimpleNamespace(extend_view=boom)
    res = _run(tool.agent_nft_inspect(InspectParams(target="3")))
    assert res.error is None and "package's lines failed: bad face" in res.extracted_content


def test_inspect_refuses_an_unpinned_collection(armed):
    tool, _ = _tool(armed, ViewChain(OTHER))
    res = _run(_no_package(tool).agent_nft_inspect(InspectParams(target=f"robinhood:{OTHER}/3")))
    assert res.error and "not a pinned collection" in res.error


def test_bind_registers_through_the_account(armed):
    tool, seen = _tool(armed, ViewChain(armed.address))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(nft="3", dry_run=True)))
    assert res.error is None, res.error
    (intent, tx, _), = seen
    assert intent.is_registration and intent.expects_mint and intent.via_account == ACCOUNT
    assert erc6551.decode_execute(tx["data"])[0].lower() == REGISTRY.lower()


def _identity_chain(owner, *, minted=True):
    base = ViewChain(owner, identities=1)
    def rpc(method, params, *args, **kw):
        if method == "eth_blockNumber":
            return hex(34_617_892)
        if method == "eth_getLogs" and params[0].get("address", "").lower() == REGISTRY.lower():
            from core.wallet.simulation import _TOPIC_TRANSFER
            return [{"address": REGISTRY, "topics": [
                _TOPIC_TRANSFER, _w(0) if minted else _w(OTHER), _w(ACCOUNT), _w(3)]}]
        if method == "eth_call" and params[0]["to"].lower() == REGISTRY.lower():
            if params[0]["data"].startswith(abi.selector("ownerOf(uint256)")):
                return _w(ACCOUNT)
        return base(method, params, *args, **kw)
    return rpc


def test_bind_refuses_a_second_identity(armed):
    tool, seen = _tool(armed, _identity_chain(armed.address))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(nft="3")))
    assert res.error and "already holds" in res.error and seen == []


def test_journal_writes_only_author_kinds(armed):
    tool, seen = _tool(armed, ViewChain(armed.address))
    res = _run(_no_package(tool).agent_nft_journal(JournalParams(kind="entry", text="x", nft="3")))
    assert res.error and "thesis or note" in res.error and seen == []


def test_journal_anchor_without_an_identity_is_skipped_and_said(armed):
    tool, _ = _tool(armed, ViewChain(armed.address))
    res = _run(_no_package(tool).agent_nft_journal(JournalParams(
        kind="thesis", text="PNL", nft="3", anchor=True, dry_run=False)))
    assert res.error is None, res.error
    assert "journal: entry #0 (thesis) signed" in res.extracted_content
    assert "anchor: skipped — the account has no identity yet" in res.extracted_content


def test_the_anchor_key_follows_the_prefix():
    from tools.agent_nft.core_verbs import anchor_key
    assert anchor_key(types.SimpleNamespace(journal_prefix="POLYROB")) == "polyrob.journal"
    assert anchor_key(types.SimpleNamespace(journal_prefix=None)) == "agent.journal"


def test_bind_refuses_a_foreign_registration_uri_before_signing(armed):
    tool, seen = _tool(armed, ViewChain(armed.address))
    result = _run(_no_package(tool).agent_nft_bind_identity(
        BindParams(nft="3", agent_uri="https://attacker.example/identity.json", dry_run=False)))
    assert "alternate agent_uri" in result.error
    assert not seen and not FakeRail.sent


def test_journal_refuses_credentials_before_any_signature(armed, monkeypatch):
    secret = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda omega"
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", secret)
    tool, seen = _tool(armed, ViewChain(armed.address))
    result = _run(_no_package(tool).agent_nft_journal(
        JournalParams(kind="note", text=secret, nft="3", dry_run=False)))
    assert "credential material" in result.error
    assert secret not in result.error
    assert not seen and not FakeRail.sent


def test_automatic_journal_entries_use_the_same_secret_check():
    from core.wallet.nft_account import build_entry
    with pytest.raises(ValueError, match="credential material"):
        build_entry(prior=[], account=ACCOUNT, chain_id=4663, kind="entry", owner=OTHER,
                    text="API_KEY=sk-" + "a" * 48)


def test_bind_ignores_an_unsolicited_identity(armed):
    tool, seen = _tool(armed, _identity_chain(armed.address, minted=False))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(nft="3")))
    assert res.error is None, res.error
    assert len(seen) == 1
