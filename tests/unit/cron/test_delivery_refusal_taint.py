"""A cron run whose money verb was refused never delivers its final text to a
PUBLIC sink (a pack channel such as the X post sink, a room, or an explicit
non-owner target). The report is re-routed to the OWNER instead, so it is not
lost. An untainted run delivers exactly as before."""
import pytest

from core.security import refusal_taint
from cron import delivery


class _Job:
    id = "job-1"
    user_id = "u1"
    task = "pnl buyback"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    refusal_taint.reset_for_tests()
    monkeypatch.setattr(delivery, "allowed_targets",
                        lambda: ("telegram", "email", "twitter"))
    monkeypatch.setattr(delivery, "router_targets", lambda: ())
    monkeypatch.setattr(delivery, "_event_log_for_read", lambda: None)
    monkeypatch.setattr(delivery, "_mark_surfaced", lambda sid, uid=None: None)
    yield
    refusal_taint.reset_for_tests()


@pytest.fixture
def sinks(monkeypatch):
    calls = {"telegram": [], "channel": []}

    async def _tg(task_agent, job, final, deliver_target):
        calls["telegram"].append(deliver_target)
        return "sent"

    async def _ch(task_agent, job, final, target):
        calls["channel"].append(target)
        return "sent"

    monkeypatch.setattr(delivery, "_deliver_telegram", _tg)
    monkeypatch.setattr(delivery, "_deliver_channel", _ch)
    return calls


@pytest.mark.asyncio
async def test_tainted_run_public_channel_goes_to_the_owner(sinks):
    refusal_taint.mark("s-1", action="defi_trade_swap")
    out = await delivery.deliver_result_ex(
        object(), _Job(), "No buyback: the guard refused.", target="twitter",
        session_id="s-1")
    assert sinks["channel"] == []
    assert sinks["telegram"] == [None]
    assert out == "sent"


@pytest.mark.asyncio
async def test_tainted_run_explicit_target_is_dropped(sinks, monkeypatch):
    monkeypatch.setenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", "true")
    refusal_taint.mark("s-1", action="defi_trade_swap")
    await delivery.deliver_result_ex(
        object(), _Job(), "No buyback.", target="telegram",
        deliver_target="-1002125904710", session_id="s-1")
    assert sinks["telegram"] == [None]


@pytest.mark.asyncio
async def test_untainted_run_delivers_as_before(sinks, monkeypatch):
    monkeypatch.setenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", "true")
    await delivery.deliver_result_ex(
        object(), _Job(), "Bought 0.01 ETH of PNL.", target="twitter", session_id="s-2")
    await delivery.deliver_result_ex(
        object(), _Job(), "Bought.", target="telegram",
        deliver_target="-1002125904710", session_id="s-2")
    assert sinks["channel"] == ["twitter"]
    assert sinks["telegram"] == ["-1002125904710"]


@pytest.mark.asyncio
async def test_another_runs_taint_does_not_apply(sinks):
    refusal_taint.mark("s-other", action="defi_trade_swap")
    await delivery.deliver_result_ex(
        object(), _Job(), "Bought.", target="twitter", session_id="s-2")
    assert sinks["channel"] == ["twitter"]
