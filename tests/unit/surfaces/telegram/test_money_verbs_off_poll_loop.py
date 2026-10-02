"""TG2 (audit 2026-10-03): every EXECUTING money verb leaves the poll loop.

Only `/bridge`, `/send`, `/swap` and `/avatar set` ran off the loop. `/lp`,
`/claim`, `/nft`, `/launch`, `/deploy`, `/identity` and `/pay` with `go` (and
their card confirms, which re-enter the same rule) awaited a receipt of up to
120 s INLINE — the whole bot froze and `/cancel` could not be read.
"""
import asyncio
from types import SimpleNamespace

import pytest

from surfaces.telegram import harness

_GO_LINES = {
    "/lp": "/lp add 0xpool 1 1 go",
    "/claim": "/claim 0xtoken go",
    "/nft": "/nft send 1 0xabc go",
    "/launch": "/launch SYM Name go",
    "/deploy": "/deploy SYM 1000 Name go",
    "/identity": "/identity register go",
    "/pay": "/pay https://x.example/r 1 go id=a1",
}


def _result(cmd, text):
    return SimpleNamespace(
        decision=SimpleNamespace(command=cmd, session_key="agent:main:telegram:dm:1:rob",
                                 session_id="s"),
        inbound=SimpleNamespace(text=text, identity=SimpleNamespace(user_id="rob")))


@pytest.mark.parametrize("cmd", sorted(_GO_LINES))
def test_an_executing_money_verb_runs_in_background(cmd):
    assert harness._runs_in_background(cmd, _result(cmd, _GO_LINES[cmd]))


@pytest.mark.parametrize("cmd", sorted(_GO_LINES))
def test_the_quote_still_answers_inline(cmd):
    quote = _GO_LINES[cmd].replace(" go", "")
    assert not harness._runs_in_background(cmd, _result(cmd, quote))


def test_a_slow_claim_go_does_not_block_the_caller(monkeypatch):
    gate = asyncio.Event()
    delivered = []

    async def _slow_admin(task_agent, result, cmd):
        await gate.wait()
        return "claimed"

    async def _deliver(text):
        delivered.append(text)

    monkeypatch.setattr(harness, "_handle_owner_admin", _slow_admin)

    async def _run():
        reply = await asyncio.wait_for(
            harness._handle_command(None, _result("/claim", _GO_LINES["/claim"]),
                                    spawn=None, deliver=_deliver), timeout=2)
        assert reply.startswith("⏳ Claim started")
        gate.set()
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.01)
        return delivered

    assert asyncio.run(_run()) == ["claimed"]
