"""115 W7: `/nft ready|unready <id> on <chain> [go]` — the owner's seat over
``agent_nft_sale_ready`` / ``agent_nft_sale_unready`` (the `/nft send` pattern: a dry run, then
`go`). The REPL and the CLI share ``nft_reply``."""
import asyncio

import pytest

from surfaces.telegram import nft_ops


def _fake(monkeypatch, name, seen, text="READY FOR SALE ... RESULT: DRY RUN"):
    async def verb(self, params, ctx):
        seen.update(verb=name, chain=params.chain, nft=params.nft, dry_run=params.dry_run, ctx=ctx)
        return type("R", (), {"error": None, "extracted_content": text})()
    monkeypatch.setattr(f"tools.agent_nft.tool.AgentNftTool.{name}", verb)


@pytest.mark.parametrize("word, name", [("ready", "agent_nft_sale_ready"),
                                        ("unready", "agent_nft_sale_unready")])
def test_the_seat_runs_the_verb_dry_then_go(monkeypatch, word, name):
    seen = {}
    _fake(monkeypatch, name, seen)
    out = asyncio.run(nft_ops.nft_reply("u_owner", [word, "7", "on", "robinhood"]))
    assert seen["verb"] == name and seen["nft"] == "7" and seen["dry_run"] is True
    assert f"/nft {word} 7 on robinhood go" in out
    asyncio.run(nft_ops.nft_reply("u_owner", [word, "7", "on", "robinhood", "go"]))
    assert seen["dry_run"] is False


def test_ready_warns_before_go(monkeypatch):
    _fake(monkeypatch, "agent_nft_sale_ready", {})
    out = asyncio.run(nft_ops.nft_reply("u_owner", ["ready", "7", "on", "robinhood"]))
    assert "plain marketplace fill can take the NFT" in out


def test_nothing_to_do_gets_no_go_hint(monkeypatch):
    _fake(monkeypatch, "agent_nft_sale_unready", {}, text="RESULT: it is not ready — no transaction.")
    out = asyncio.run(nft_ops.nft_reply("u_owner", ["unready", "7", "on", "robinhood"]))
    assert "go" not in out.split("no transaction")[-1]


def test_the_seat_names_the_usage():
    out = asyncio.run(nft_ops.nft_reply("u_owner", ["ready", "on", "robinhood"]))
    assert "Usage: /nft ready <id|collection#id> on <chain> [go]" in out
    assert "/nft ready <id|collection#id>" in nft_ops.USAGE and "/nft unready" in nft_ops.USAGE
