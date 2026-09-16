import asyncio
import multiprocessing

import pytest

from core.wallet.audit_sink import JsonlAuditSink
from core.wallet.policy import PolicyGate


def _spend(path, ready, start, results):
    gate = PolicyGate(100, audit_sink=JsonlAuditSink(path), daily_cap_usd=10)
    ready.put(True)
    start.wait(10)

    async def run():
        async with gate.reserve():
            allowed = gate.check(venue="x402", amount_usd=8, idempotency_key=None).allowed
            if allowed:
                await asyncio.sleep(0.15)
                gate.record(venue="x402", action="pay", amount_usd=8,
                            counterparty=None, idempotency_key=None, result_ref=None)
            results.put(allowed)

    asyncio.run(run())


def test_two_processes_cannot_both_clear_shared_daily_cap(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_spend, args=(str(tmp_path / "audit.jsonl"), ready, start, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=15)
        start.set()
        assert sorted(results.get(timeout=15) for _ in workers) == [False, True]
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)


@pytest.mark.parametrize("damage", ["truncate", "delete", "corrupt"])
def test_damaged_ledger_refuses_new_spend(tmp_path, damage):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    gate = PolicyGate(100, audit_sink=sink, daily_cap_usd=10)
    gate.record(venue="x402", action="pay", amount_usd=8,
                counterparty=None, idempotency_key=None, result_ref=None)
    if damage == "delete":
        path.unlink()
    elif damage == "truncate":
        path.write_text("")
    else:
        with path.open("a") as stream:
            stream.write("not-json\n")
    assert not gate.check(venue="x402", amount_usd=1, idempotency_key=None).allowed
    reloaded = PolicyGate(100, audit_sink=JsonlAuditSink(str(path)), daily_cap_usd=10)
    assert not reloaded.check(venue="x402", amount_usd=1, idempotency_key=None).allowed


def test_interleaved_appends_do_not_skip_other_writers(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    first, second = JsonlAuditSink(path), JsonlAuditSink(path)
    first.append({"ts": 1, "amount_usd": 8})
    second.append({"ts": 2, "amount_usd": 7})
    first.refresh()
    assert [e["amount_usd"] for e in first] == [8, 7]
    assert [e["amount_usd"] for e in second] == [8, 7]


def test_factory_does_not_downgrade_failed_storage(monkeypatch):
    from core.wallet.factory import _durable_audit_sink

    def fail():
        raise OSError("unwritable ledger")

    monkeypatch.setattr("core.wallet.audit_sink.default_audit_sink", fail)
    with pytest.raises(OSError):
        _durable_audit_sink()


@pytest.mark.parametrize("tail", [b'{"amount_usd": 9', b'\xff\n'])
def test_incomplete_or_non_utf8_ledger_refuses_spend(tmp_path, tail):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(str(path))
    path.write_bytes(tail)
    gate = PolicyGate(100, audit_sink=sink, daily_cap_usd=10)
    assert not gate.check(venue="x402", amount_usd=1, idempotency_key=None).allowed
    assert not JsonlAuditSink(str(path)).healthy


@pytest.mark.parametrize("marker", ["", "bad", "-1"])
def test_invalid_high_water_marker_refuses_spend(tmp_path, marker):
    path = tmp_path / "audit.jsonl"
    path.with_suffix(".jsonl.hwm").write_text(marker)
    assert not JsonlAuditSink(str(path)).healthy


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_clear_active_reservation(tmp_path):
    sink = JsonlAuditSink(str(tmp_path / "audit.jsonl"))

    async def wait_for_lock():
        async with sink.reserve():
            pytest.fail("second reservation entered while first holds lock")

    async with sink.reserve():
        held_fd = sink._reservation_fd
        waiter = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert sink._reservation_fd == held_fd
        sink.append({"ts": 1, "amount_usd": 1})
    assert sink._reservation_fd is None


@pytest.mark.asyncio
async def test_reservation_cannot_be_borrowed_by_another_task_or_thread(tmp_path):
    import asyncio
    from core.wallet.audit_sink import JsonlAuditSink
    sink = JsonlAuditSink(str(tmp_path / 'audit.jsonl'))
    entry = {'ts': 1, 'amount_usd': 1, 'venue': 'test'}
    async with sink.reserve():
        async def unrelated():
            with pytest.raises(RuntimeError, match='another task'):
                sink.append(entry)
        await asyncio.create_task(unrelated())
        with pytest.raises(RuntimeError, match='another task'):
            await asyncio.to_thread(sink.append, entry)
        assert len(sink) == 0
        sink.append(entry)  # The actual reservation owner can record.
    sink.append(entry)  # Ordinary writes work after release.
    assert len(JsonlAuditSink(str(tmp_path / 'audit.jsonl'))) == 2
