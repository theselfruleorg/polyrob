"""050 §7.6 — the core `agent_nft` tool. Its only jobs: the flag, the turn origin (+ pause for a
live write), and running the core verb body (123 S4: there is no package seam)."""
import asyncio
import types

from tools.agent_nft.tool import AgentNftTool, JournalParams, SnapshotParams


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _err(res):
    return getattr(res, "error", None) or (res.get("error") if isinstance(res, dict) else None)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED", raising=False)
    res = _run(AgentNftTool().agent_nft_snapshot(SnapshotParams(chain="robinhood")))
    assert "AGENT_NFT_ENABLED" in _err(res)


def test_the_mint_needs_no_package(monkeypatch):
    """123 S3: the mint body is core — a missing package is never the refusal."""
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.split(".")[0] in ("polyrob_drop", "polyrob_desk"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from tools.agent_nft.tool import MintParams
    res = _run(AgentNftTool().agent_nft_collection_mint(MintParams(chain="robinhood", max_spend_usd=1.0)))
    assert "polyrob_drop" not in _err(res) and "package" not in _err(res)


def test_core_names_no_agent_nft_package():
    """123 S4: the import-by-name seam is gone."""
    import core.tool_capabilities as caps
    assert not hasattr(caps, "AGENT_NFT_PACKAGE_MODULES")


def test_params_reach_the_core_mint_body(monkeypatch):
    """123 S3: the mint body is tools/agent_nft/mint.py."""
    from tools.agent_nft import mint as mint_mod
    from tools.agent_nft.tool import MintParams
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    seen = {}

    async def mint(tool, params, ctx):
        seen["params"] = params
        return tool._ar(content="ok")
    monkeypatch.setattr(mint_mod, "mint", mint)
    res = _run(AgentNftTool().agent_nft_collection_mint(MintParams(chain="robinhood", max_spend_usd=1.0)))
    assert seen["params"].dry_run is True and not _err(res)


def test_a_verb_exception_is_an_error_result(monkeypatch):
    from tools.agent_nft import mint as mint_mod
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")

    from tools.agent_nft.tool import MintParams

    async def mint(tool, params, ctx):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(mint_mod, "mint", mint)
    res = _run(AgentNftTool().agent_nft_collection_mint(MintParams(chain="robinhood", max_spend_usd=1.0)))
    assert "rpc down" in _err(res)


def test_mint_params_are_the_blind_mint():
    """The collection's mint: mint(to, qty, minPnlOutPerToken); no face is chosen."""
    import pytest
    from pydantic import ValidationError

    from tools.agent_nft.tool import MintParams
    p = MintParams(chain="robinhood", max_spend_usd=200.0)
    assert (p.to, p.qty, p.min_out_per_token, p.chain) == (None, 1, None, "robinhood")
    with pytest.raises(ValidationError):  # the old name is gone
        MintParams(chain="robinhood", max_spend_usd=1.0, min_pnl_out=5)
    assert MintParams(qty=10, chain="robinhood-testnet", max_spend_usd=1.0).chain == "robinhood-testnet"
    with pytest.raises(ValidationError):  # there is no default chain
        MintParams(max_spend_usd=1.0)
    for bad in ({"qty": 0}, {"qty": 101}, {"min_pnl_out": -1}, {"min_out_per_token": -1}, {"chain": "solana"}, {"seed": "POLYROB"},
                {"to": "not-an-address"}):
        with pytest.raises(ValidationError):
            MintParams(**{"chain": "robinhood", "max_spend_usd": 1.0, **bad})



def test_mint_params_carry_the_whitelist_allowance_and_proof():
    """115 M4: the sale's mintWL needs (allowance, proof); the package verb reads these names."""
    import pytest
    from pydantic import ValidationError

    from tools.agent_nft.tool import MintParams
    p = MintParams(chain="robinhood", max_spend_usd=1.0)
    assert (p.allowance, p.proof) == (None, None)
    proof = ["0x" + "ab" * 32, "0x" + "CD" * 32]
    p = MintParams(chain="robinhood", max_spend_usd=1.0, allowance=3, proof=proof)
    assert (getattr(p, "allowance"), getattr(p, "proof")) == (3, proof)
    assert len(MintParams(chain="robinhood", max_spend_usd=1.0, proof=["0x" + "11" * 32] * 32).proof) == 32
    for bad in ({"allowance": 0}, {"proof": ["0xab"]}, {"proof": ["ab" * 32]},
                {"proof": ["0x" + "11" * 32] * 33}, {"proof": "0x" + "11" * 32}):
        with pytest.raises(ValidationError):
            MintParams(**{"chain": "robinhood", "max_spend_usd": 1.0, **bad})


def test_the_mint_text_names_the_sale_not_a_fixed_price():
    import inspect as _inspect
    src = _inspect.getsource(AgentNftTool.agent_nft_collection_mint)
    from tools.agent_nft.tool import MintParams
    text = src + str(MintParams.model_json_schema())
    assert "0.033" not in text and "sale" in text


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
        with pytest.raises(ValidationError):  # there is no default chain
            model(**base)
        p = model(chain="robinhood", **base)
        assert (p.chain, p.nft, p.account) == ("robinhood", None, None)
        p = model(chain="robinhood-testnet", nft="0x" + "c0" * 20 + "#7", account=acct, **base)
        assert (p.chain, p.nft, p.account) == ("robinhood-testnet", "0x" + "c0" * 20 + "#7", acct)
        with pytest.raises(ValidationError):
            model(chain="robinhood", account="not-an-address", **base)
        with pytest.raises(ValidationError):   # N3: any EVM chain of the table, not a list
            model(chain="no-such-chain", **base)
        assert model(chain=" Base ", **base).chain == "base"
    seen = {}

    async def snapshot(tool, params, ctx):
        seen["params"] = params
        return tool._ar(content="ok")
    from tools.agent_nft import core_verbs
    monkeypatch.setattr(core_verbs, "snapshot", snapshot)
    _run(AgentNftTool().agent_nft_snapshot(SnapshotParams(chain="robinhood", nft="7")))
    assert seen["params"].nft == "7"
