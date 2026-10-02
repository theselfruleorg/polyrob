"""050 §7.6 — the thin core `agent_nft` tool. Its only jobs: the flag, the turn origin
(+ pause for a live write), a clean refusal when the package is absent, and handing
validated params to the agent-NFT package's `verbs.<verb>` (`polyrob_drop`, with the legacy
package name as a fallback)."""
import asyncio
import types

from tools.agent_nft.tool import AgentNftTool, JournalParams, SnapshotParams


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _err(res):
    return getattr(res, "error", None) or (res.get("error") if isinstance(res, dict) else None)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED", raising=False)
    res = _run(AgentNftTool().agent_nft_snapshot(SnapshotParams()))
    assert "AGENT_NFT_ENABLED" in _err(res)


def test_missing_package_is_a_refusal_with_a_remedy(monkeypatch):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    import builtins
    real = builtins.__import__

    from core.tool_capabilities import AGENT_NFT_PACKAGE_MODULES

    def fake(name, *a, **k):
        if name.split(".")[0] in AGENT_NFT_PACKAGE_MODULES:
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from tools.agent_nft.tool import MintParams
    res = _run(AgentNftTool().agent_nft_collection_mint(MintParams(max_spend_usd=1.0)))
    assert "polyrob_drop" in _err(res)
    assert "polyrob[drop]" not in _err(res)  # no such extra until the dist publishes
    assert "theselfruleorg" not in _err(res)


def test_the_package_is_found_under_its_new_name_or_the_legacy_one(monkeypatch):
    import sys
    from core.tool_capabilities import AGENT_NFT_PACKAGE_MODULES
    new, legacy = AGENT_NFT_PACKAGE_MODULES
    for present in (new, legacy):
        for name in AGENT_NFT_PACKAGE_MODULES:
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.delitem(sys.modules, name + ".verbs", raising=False)
        # Hide the OTHER name: a real package on the path must not win (None = ImportError).
        for name in AGENT_NFT_PACKAGE_MODULES:
            if name != present:
                monkeypatch.setitem(sys.modules, name, None)
                monkeypatch.setitem(sys.modules, name + ".verbs", None)
        verbs = types.ModuleType(present + ".verbs")
        pkg = types.ModuleType(present)
        pkg.verbs = verbs
        monkeypatch.setitem(sys.modules, present, pkg)
        monkeypatch.setitem(sys.modules, present + ".verbs", verbs)
        assert AgentNftTool()._impl() is verbs
        monkeypatch.delitem(sys.modules, present)
        monkeypatch.delitem(sys.modules, present + ".verbs")


def test_params_reach_the_package_verb(monkeypatch):
    """mint is a package verb (C20 keeps it there)."""
    from tools.agent_nft.tool import MintParams
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    seen = {}

    async def mint(tool, params, ctx):
        seen["params"] = params
        return tool._ar(content="ok")
    tool = AgentNftTool(impl=types.SimpleNamespace(mint=mint))
    res = _run(tool.agent_nft_collection_mint(MintParams(max_spend_usd=1.0)))
    assert seen["params"].dry_run is True and not _err(res)


def test_a_verb_exception_is_an_error_result(monkeypatch):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")

    from tools.agent_nft.tool import MintParams

    async def mint(tool, params, ctx):
        raise RuntimeError("rpc down")
    res = _run(AgentNftTool(impl=types.SimpleNamespace(mint=mint)).agent_nft_collection_mint(
        MintParams(max_spend_usd=1.0)))
    assert "rpc down" in _err(res)


def test_mint_params_are_the_blind_mint():
    """The collection's mint: mint(to, qty, minPnlOutPerToken); no face is chosen."""
    import pytest
    from pydantic import ValidationError

    from tools.agent_nft.tool import MintParams
    p = MintParams(max_spend_usd=200.0)
    assert (p.to, p.qty, p.min_pnl_out, p.chain) == (None, 1, 0, "robinhood")
    assert MintParams(qty=10, chain="robinhood-testnet", max_spend_usd=1.0).chain == "robinhood-testnet"
    for bad in ({"qty": 0}, {"qty": 11}, {"min_pnl_out": -1}, {"chain": "base"}, {"seed": "POLYROB"},
                {"to": "not-an-address"}):
        with pytest.raises(ValidationError):
            MintParams(max_spend_usd=1.0, **bad)


def test_descriptions_claim_only_what_ships():
    """core evaluation handoff §5 W10: no ERC-8217 write exists; revoke_all does not revoke everything."""
    import inspect as _inspect

    src = _inspect.getsource(AgentNftTool)
    assert "8217" not in src
    assert "Revoke EVERY" not in src and "safe to sell" not in src
    assert "NOT revoked" in src


def test_c10_snapshot_journal_and_bind_name_the_nft(monkeypatch):
    """C10: two NFTs on one chain must be nameable on every per-NFT verb."""
    import pytest
    from pydantic import ValidationError

    from tools.agent_nft.tool import BindParams
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    acct = "0x" + "ab" * 20
    for model, base in ((SnapshotParams, {}), (JournalParams, {"kind": "note", "text": "hi"}),
                        (BindParams, {})):
        p = model(**base)
        assert (p.chain, p.nft, p.account) == ("robinhood", None, None)
        p = model(chain="robinhood-testnet", nft="0x" + "c0" * 20 + "#7", account=acct, **base)
        assert (p.chain, p.nft, p.account) == ("robinhood-testnet", "0x" + "c0" * 20 + "#7", acct)
        with pytest.raises(ValidationError):
            model(account="not-an-address", **base)
        with pytest.raises(ValidationError):
            model(chain="base", **base)
    seen = {}

    async def snapshot(tool, params, ctx):
        seen["params"] = params
        return tool._ar(content="ok")
    from tools.agent_nft import core_verbs
    monkeypatch.setattr(core_verbs, "snapshot", snapshot)
    _run(AgentNftTool().agent_nft_snapshot(SnapshotParams(nft="7")))
    assert seen["params"].nft == "7"
