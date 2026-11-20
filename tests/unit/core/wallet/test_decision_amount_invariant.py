"""Proposal 125 S9 B-M6: every allowed or ``owner_queue`` Decision carries a
finite ``amount_usd``.

The guard pins it by construction: step 7 refuses a ``None`` and a non-finite
figure before it stores ``c.amount_usd``, and every allowing or owner-queue
``Decision(...)`` passes that stored figure. A caller never repairs a missing
figure to ``0.0`` (the dapp bridge refuses instead).
"""
import ast
import json
import math
import pathlib

import pytest

from core.wallet.tx_guard import Decision

ROOT = pathlib.Path(__file__).resolve().parents[4]
GUARD_FILES = [ROOT / "core/wallet/tx_guard.py",
               *sorted((ROOT / "core/wallet/tx_guard_steps").glob("*.py"))]


def _is_decision(node):
    return (isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "Decision")


def _allows_or_queues(call):
    first = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == "allowed"), None)
    plain_refusal = isinstance(first, ast.Constant) and first.value is False
    lane = next((k.value for k in call.keywords if k.arg == "lane"), None)
    queues = isinstance(lane, ast.Constant) and lane.value == "owner_queue"
    dynamic_lane = lane is not None and not isinstance(lane, ast.Constant)
    return not plain_refusal or queues or dynamic_lane


def _functions():
    for path in GUARD_FILES:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path, fn


def test_every_allowing_or_queueing_decision_passes_the_validated_figure():
    seen = 0
    for path, fn in _functions():
        for call in (n for n in ast.walk(fn) if _is_decision(n) and _allows_or_queues(n)):
            seen += 1
            kw = {k.arg: k.value for k in call.keywords}
            where = f"{path.relative_to(ROOT)}:{call.lineno}"
            assert "amount_usd" in kw, f"{where}: an allowing Decision without amount_usd"
            val = kw["amount_usd"]
            assert isinstance(val, ast.Name) and val.id == "amount_usd", (
                f"{where}: amount_usd must be the step-7 validated figure, "
                f"got {ast.unparse(val)}")
            # The local name is read from the context step 7 filled.
            reads = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "amount_usd"
                             for t in n.targets)]
            assert reads and all(ast.unparse(r.value) == "c.amount_usd" for r in reads), (
                f"{where}: amount_usd is not read from c.amount_usd")
    assert seen >= 4, "the guard's allowing Decisions moved; update this pin"


def test_step_7_refuses_a_missing_or_non_finite_figure_before_storing_it():
    fn = next(f for _p, f in _functions() if f.name == "step_7_price")
    src = ast.unparse(fn)
    none_at = src.index("if amount_usd is None:")
    finite_at = src.index("math.isfinite(amount_usd)")
    store_at = src.index("c.amount_usd = amount_usd")
    assert none_at < store_at and finite_at < store_at


# -- the dapp bridge refuses a figure-less verdict (no `or 0.0`) -------------

@pytest.mark.asyncio
@pytest.mark.parametrize("lane,allowed", [("autonomous", True), ("owner_queue", False)])
@pytest.mark.parametrize("usd", [None, math.nan, math.inf])
async def test_the_dapp_bridge_refuses_a_verdict_without_a_finite_figure(lane, allowed, usd):
    from tests.unit.tools.dapp_browser import test_bridge as tb
    from tools.dapp_browser import bridge as B
    approver = tb._Approver(True)
    bridge, gate = tb._bridge(allow=allowed, lane=lane, usd=usd, approver=approver)
    tb._Rail.last = None
    out = await tb._ask(bridge, "eth_sendTransaction",
                        [{"to": tb.POOL, "data": "0x01", "value": "0x0"}])
    assert out["error"]["code"] == B.USER_REJECTED
    assert "no USD value" in out["error"]["message"]
    assert approver.asked == [], "the owner is never asked at a made-up $0"
    assert gate.recorded == [] and bridge.envelope.spent_usd == 0.0
    assert tb._Rail.last is None or tb._Rail.last.sent is False


def test_a_decision_default_is_a_refusal_without_a_figure():
    d = Decision(False, "x")
    assert d.lane == "refuse" and d.amount_usd is None
    json.dumps({"lane": d.lane})
