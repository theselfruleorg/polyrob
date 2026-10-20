"""085 W6 at the verb: B-2 (baseline re-read inside the reservation), B-3 (a
reused request id refuses before broadcast), L-6 (a stale unsent row stops
blocking) and the natural bridge path unchanged."""
import time

from core.wallet import bridge_guard
from tests.unit.tools.defi import test_bridge_evm_origin_e2e as be
from tests.unit.tools.defi.test_bridge_evm_origin_e2e import (  # noqa: F401
    bound_wallet_owner, rig)


def _db(rig):
    return str(rig.tmp / "bridges.db")


def test_the_natural_bridge_path_is_unchanged(rig):
    r = be._run(be._Tool(), be._params())
    text = r.content or r.error or ""
    assert "BROADCAST: 0xbeef" in text
    assert "phase 2 : ARRIVED" in text, text
    rows = bridge_guard.open_bridges("owner", db_path=_db(rig))
    assert rows == []


def test_a_reused_request_id_refuses_before_broadcast(rig):
    """B-3: the provider hands back a request id another recorded bridge
    already holds. `INSERT OR REPLACE` erased that row; now nothing is sent."""
    earlier = bridge_guard.record_pending(
        user_id="owner", quote=be._quote(dest_chain_id=8453), amount_usd=1.0,
        balance_before=0, db_path=_db(rig))
    bridge_guard.settle(earlier, state=bridge_guard.STATE_ARRIVED, detail="done",
                        db_path=_db(rig))
    r = be._run(be._Tool(), be._params())
    text = r.content or r.error or ""
    assert "RESULT: NOT SENT" in text, text
    assert "already recorded" in text
    assert all(i.sent is None for i in be._Rail.instances), "nothing may broadcast"


def test_the_baseline_is_re_read_inside_the_reservation(rig, monkeypatch):
    """B-2: an inflow during the owner wait must not count as the arrival."""
    reads = iter([10 ** 15, 10 ** 15 + 7, 10 ** 15 + 7 + be.OUT_RAW])
    monkeypatch.setattr(bridge_guard, "native_balance_raw",
                        lambda addr, chain: next(reads, 10 ** 15 + 7 + be.OUT_RAW))
    recorded = {}
    real = bridge_guard.record_pending

    def _spy(**kw):
        recorded["before"] = kw["balance_before"]
        return real(**kw)
    monkeypatch.setattr(bridge_guard, "record_pending", _spy)
    r = be._run(be._Tool(), be._params())
    assert "phase 2 : ARRIVED" in (r.content or ""), r.content or r.error
    assert recorded["before"] == 10 ** 15 + 7


def test_a_stale_unsent_row_no_longer_blocks_the_balance(rig, monkeypatch):
    """L-6: a crash between record and broadcast left a `pending` row with no
    tx. After an hour it is closed as never sent, and the next bridge runs."""
    from core.sqlite_util import execute_retry
    stuck = bridge_guard.record_pending(
        user_id="owner", quote=be._quote(request_id="0xcrashed"), amount_usd=1.0,
        balance_before=0, db_path=_db(rig))
    execute_retry(_db(rig), "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - bridge_guard.UNSENT_STALE_SEC - 60, stuck))
    monkeypatch.setattr(be._Provider, "status", lambda self, rid: (
        ("pending", "waiting") if rid == "0xcrashed" else self._status))
    r = be._run(be._Tool(), be._params())
    text = r.content or r.error or ""
    assert "BROADCAST: 0xbeef" in text, text



def test_the_verb_tells_the_owner_about_a_row_it_closes_as_never_sent(rig, monkeypatch):
    """The verb sweeps too (the watcher can be off): what it closes reaches the
    owner once, through the same bridge notice, naming the origin chain."""
    from core.sqlite_util import execute_retry
    from tools.defi import bridge_verb
    seen = []

    async def _notify(tool, ctx, notice, *, settled):
        seen.append(notice)
    monkeypatch.setattr(bridge_verb, "_notify", _notify)
    stuck = bridge_guard.record_pending(
        user_id="owner", quote=be._quote(request_id="0xcrashed"), amount_usd=1.0,
        balance_before=0, db_path=_db(rig))
    execute_retry(_db(rig), "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - bridge_guard.UNSENT_STALE_SEC - 60, stuck))
    monkeypatch.setattr(be._Provider, "status", lambda self, rid: (
        ("pending", "waiting") if rid == "0xcrashed" else self._status))
    r = be._run(be._Tool(), be._params())
    assert "BROADCAST: 0xbeef" in (r.content or r.error or "")
    never = [n for n in seen if n.state == "never_sent"]
    assert len(never) == 1
    assert never[0].extra_refs == ("0xcrashed", stuck)
    assert never[0].chain == bridge_guard.chain_name_for_id(be._quote().origin_chain_id)
