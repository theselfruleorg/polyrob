"""098 close-out MO-01: the deposit sweep is an explicit machine on
crypto_payments.sweep_state — claimed whole before broadcast, a remote-signer
sweep with no receipt is swept_unproven and never booked on the mark, a sweep
stuck in `sweeping` reaches the owner once and is never resent, and the owner
reconcile report is read-only."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.state.machine import IllegalTransition
from modules.payments import sweep_state
from modules.payments.treasury_sweeper import TreasurySweeper
from tests.unit.modules.payments.test_treasury_submission_safety import ADDRESS, _RigDB


def _sweeper(db, wallet, *, latest=0, mark=0):
    """A remote-signer sweeper reads the latest balance against the credited
    mark first; by default both are 0 (everything there is credited)."""
    s = TreasurySweeper(db, wallet, SimpleNamespace(
        treasury_address='0x' + '2' * 40, ethereum_rpc_url='https://example.invalid'))
    if getattr(wallet, 'remote', False):
        _with_mark(db, mark)

        async def _latest(*a, **k):
            return latest
        s._latest_balance = _latest
    return s


def _deposit(rid=1):
    return dict(id=rid, user_id='test', chain='ethereum', user_address=ADDRESS,
                deposit_address=ADDRESS, token_symbol='ETH', amount_usd=100)


@pytest.mark.asyncio
async def test_remote_signer_sweep_is_unproven_and_never_booked(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(return_value='0x' + 'ab' * 32))
    booked = []
    monkeypatch.setattr('modules.payments.deposit_monitor.book_sweep_outflow',
                        lambda *a, **k: booked.append(a))
    await _sweeper(db, wallet)._sweep_deposit(_deposit())
    row = db.row()
    assert (row['sweep_state'], row['sweep_tx_hash']) == ('swept_unproven', '0x' + 'ab' * 32)
    assert row['swept_at'] is not None and booked == []
    wallet.sweep.assert_called_once()


@pytest.mark.asyncio
async def test_remote_signer_error_keeps_the_claim_and_is_not_resent(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(side_effect=TimeoutError('no reply')))
    s = _sweeper(db, wallet)
    await s._sweep_deposit(_deposit())
    assert db.row()['sweep_state'] == 'sweeping'      # outcome unknown
    await s._sweep_deposit(_deposit())                 # the held group is skipped
    assert wallet.sweep.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('err', [
    ('refused', 'guard_refused'), ('refused', 'broadcast_failed'),
    ('refused', 'not_configured'), ('refused', 'nonce_reuse'), ('unsent', None)])
async def test_remote_signer_definite_refusal_releases_the_group(monkeypatch, tmp_path, err):
    """098 review: the signer sends nothing before a definite refusal, so the
    group goes back to unswept and the next tick retries (it used to stay
    `sweeping` and block every later deposit of the user/chain/token)."""
    from core.signer.client import SignerRefused, SignerUnavailable
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    exc = (SignerRefused(err[1], 'not sent') if err[0] == 'refused'
           else SignerUnavailable('no socket', sent=False))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(side_effect=exc))
    s = _sweeper(db, wallet)
    await s._sweep_deposit(_deposit())
    assert db.row()['sweep_state'] == 'unswept'
    await s._sweep_deposit(_deposit())
    assert wallet.sweep.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['internal', 'sent'])
async def test_remote_signer_unknown_outcome_keeps_the_claim(monkeypatch, tmp_path, kind):
    from core.signer.client import SignerRefused, SignerUnavailable
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    exc = (SignerRefused('internal', 'boom') if kind == 'internal'
           else SignerUnavailable('read timed out', sent=True))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(side_effect=exc))
    await _sweeper(db, wallet)._sweep_deposit(_deposit())
    assert db.row()['sweep_state'] == 'sweeping'


@pytest.mark.asyncio
async def test_claim_is_all_or_nothing():
    db = _RigDB()
    db.conn.execute("INSERT INTO crypto_payments (id, user_id, chain, token_symbol, status, "
                    "sweep_state) VALUES (2, 'test', 'ethereum', 'ETH', 'confirmed', 'sweeping')")
    assert not await sweep_state.claim_group(db, (1, 2), 100)
    assert db.row(1)['sweep_state'] == 'unswept' and db.row(2)['sweep_state'] == 'sweeping'
    db.conn.execute("UPDATE crypto_payments SET sweep_state = 'sweep_failed' WHERE id = 2")
    assert await sweep_state.claim_group(db, (1, 2), 101)     # a failed row joins the group
    assert {db.row(i)['sweep_state'] for i in (1, 2)} == {'sweeping'}


@pytest.mark.asyncio
async def test_swept_is_terminal():
    db = _RigDB()
    with pytest.raises(IllegalTransition):      # strict mode in tests
        await sweep_state.move_group(db, (1,), 'sweeping', ('swept',), started_at=1)


@pytest.mark.asyncio
async def test_stuck_sweep_reaches_the_owner_once_and_stays(monkeypatch):
    events = []
    monkeypatch.setattr('core.event_log.emit', lambda kind, **kw: events.append((kind, kw)))
    db = _RigDB()
    t0 = int(time.time())
    assert await sweep_state.claim_group(db, (1,), t0)
    assert await sweep_state.reap_stuck_sweeps(db, now=t0 + 60) == []
    out = await sweep_state.reap_stuck_sweeps(db, now=t0 + 3700)
    assert [r['id'] for r in out] == [1]
    assert await sweep_state.reap_stuck_sweeps(db, now=t0 + 7200) == []
    assert [k for k, _ in events] == ['payment_deposit_sweep_stuck']
    assert db.row()['sweep_state'] == 'sweeping'      # never released, never resent


@pytest.mark.asyncio
async def test_owner_report_is_read_only(monkeypatch):
    db = _RigDB()
    db.conn.execute("INSERT INTO crypto_payments (id, user_id, chain, token_symbol, status, "
                    "amount_usd, sweep_state, swept_at) VALUES (2, 'test', 'ethereum', 'ETH', "
                    "'confirmed', 40, 'swept_unproven', '2026-11-01')")
    db.conn.execute("CREATE TABLE deposit_balance_marks (user_id TEXT, chain TEXT, "
                    "token_symbol TEXT, balance TEXT, as_of_block INTEGER)")
    db.conn.execute("INSERT INTO deposit_balance_marks VALUES ('test','ethereum','ETH','7',12)")
    before = [dict(r) for r in db.conn.execute("SELECT * FROM crypto_payments")]

    async def onchain(user, chain, token):
        return '5'
    rep = await sweep_state.deposit_report(db, onchain=onchain)
    assert len(rep) == 1
    item = rep[0]
    assert item['states'] == {'unswept': {'count': 1, 'usd': 100.0},
                              'swept_unproven': {'count': 1, 'usd': 40.0}}
    assert (item['mark_balance'], item['mark_as_of_block'], item['onchain_balance']) == \
        ('7', 12, '5')
    assert item['needs_owner'] and item['credited_usd'] == 140.0
    assert [dict(r) for r in db.conn.execute("SELECT * FROM crypto_payments")] == before


def test_owner_deposits_cli_lists_states_and_flags(monkeypatch):
    import asyncio
    from click.testing import CliRunner
    from cli.commands import owner_money
    from cli.commands.owner import owner
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET sweep_state = 'sweeping'")
    monkeypatch.setattr(owner_money, "_run", lambda f: (True, asyncio.run(f(db))))
    out = CliRunner().invoke(owner, ["deposits"]).output
    assert "test ethereum ETH: credited $100.00 — sweeping 1 ($100.00); no mark" in out
    assert "needs you" in out


@pytest.mark.asyncio
async def test_backfill_survives_a_crash_after_the_alter():
    """098 review: the column was added but the backfill UPDATE never ran (a
    crash between the two statements) — the next call still marks legacy
    swept rows `swept`."""
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET swept_at='2026-01-01', sweep_state='unswept'")
    await sweep_state.ensure_sweep_schema(db)
    assert db.row()['sweep_state'] == 'swept'


@pytest.mark.asyncio
async def test_marks_schema_checks_before_the_alter():
    from modules.payments.deposit_monitor import ensure_marks_schema
    db = _RigDB()
    seen = []
    real = db.execute

    async def spy(sql, params=()):
        seen.append(sql)
        return await real(sql, params)
    db.execute = spy
    await ensure_marks_schema(db)
    await ensure_marks_schema(db)
    assert sum("ALTER TABLE deposit_balance_marks" in s for s in seen) == 1


@pytest.mark.asyncio
async def test_owner_resolves_a_stuck_sweep_and_the_tx_is_kept(monkeypatch, tmp_path):
    """098 review O-02: an unknown-outcome broadcast keeps its tx hash; the
    owner releases (not sent) or records (sent) the group."""
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    db = _RigDB()
    assert await sweep_state.claim_group(db, (1,), 100)
    assert await sweep_state.note_sweep_tx(db, (1,), 100, '0x' + 'cd' * 32) == 1
    assert db.row()['sweep_tx_hash'] == '0x' + 'cd' * 32
    assert await sweep_state.resolve_stuck_sweep(db, 1, sent=False) == 1
    assert db.row()['sweep_state'] == 'unswept'
    assert await sweep_state.resolve_stuck_sweep(db, 1, sent=False) == 0   # not sweeping
    assert await sweep_state.claim_group(db, (1,), 200)
    assert await sweep_state.resolve_stuck_sweep(db, 1, sent=True, tx_hash='0x' + 'ef' * 32) == 1
    row = db.row()
    assert (row['sweep_state'], row['sweep_tx_hash']) == ('swept_unproven', '0x' + 'ef' * 32)


def test_deposit_resolve_cli_needs_an_outcome_and_confirm():
    from click.testing import CliRunner
    from cli.commands.owner import owner
    r = CliRunner().invoke(owner, ["deposit-resolve", "1", "--not-sent"])
    assert r.exit_code != 0 and "--confirm" in r.output
    r = CliRunner().invoke(owner, ["deposit-resolve", "1", "--confirm"])
    assert r.exit_code != 0 and "--sent or --not-sent" in r.output


def test_deposit_resolve_names_an_open_wallet_submission(monkeypatch):
    import asyncio
    from click.testing import CliRunner
    from cli.commands import owner_money
    from cli.commands.owner import owner
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET sweep_state = 'sweeping'")
    monkeypatch.setattr(owner_money, "_run", lambda f: (True, asyncio.run(f(db))))
    monkeypatch.setattr("core.wallet.submission_journal.unresolved",
                        lambda data_dir=None: [{"tx_hash": "0xfeed", "state": "prepared"}])
    rid = db.conn.execute("SELECT id FROM crypto_payments LIMIT 1").fetchone()[0]
    out = CliRunner().invoke(owner, ["deposit-resolve", str(rid), "--not-sent",
                                     "--confirm"]).output
    assert "released to unswept" in out
    assert "open wallet submission 0xfeed (prepared)" in out and "release-submission" in out


def test_deposit_resolve_sent_without_a_tx_is_a_usage_error(monkeypatch):
    import asyncio
    from click.testing import CliRunner
    from cli.commands import owner_money
    from cli.commands.owner import owner
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET sweep_state = 'sweeping'")
    monkeypatch.setattr(owner_money, "_run", lambda f: (True, asyncio.run(f(db))))
    rid = db.conn.execute("SELECT id FROM crypto_payments LIMIT 1").fetchone()[0]
    r = CliRunner().invoke(owner, ["deposit-resolve", str(rid), "--sent", "--confirm"])
    assert r.exit_code != 0 and "--tx" in r.output and "Traceback" not in r.output


# -- the credited row carries the deposit address ---------------------------

@pytest.mark.asyncio
async def test_a_credited_reading_carries_its_deposit_address(monkeypatch):
    """The sweeper refuses a row whose deposit_address does not match the
    derived signer; an empty one blocked every group for ever."""
    import sys
    from tests.unit.modules.payments.test_deposit_monitor_notify import (
        _Config, _FakeBalanceManager, _FakeDB, _fake_web3_module)
    from modules.payments.deposit_monitor import DepositMonitor
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(2 * 10**18))
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "3000.0")
    monitor = DepositMonitor(_FakeDB(), _FakeBalanceManager(), _Config())
    readings = await monitor._check_chain_deposits(
        ADDRESS, "ethereum", {"rpc_url": "http://fake-rpc.invalid", "chain_id": 1})
    assert readings and all(r["address"] == ADDRESS for r in readings)


@pytest.mark.asyncio
async def test_ensure_sweep_schema_backfills_an_empty_deposit_address():
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET deposit_address = ''")
    db.conn.execute("INSERT INTO crypto_payments (id, user_id, chain, deposit_address, "
                    "token_symbol, amount_usd, status) VALUES (2, 'other', 'ethereum', '', "
                    "'ETH', 5, 'confirmed')")
    db.conn.execute("CREATE TABLE user_deposit_addresses (user_id TEXT PRIMARY KEY, "
                    "deposit_address TEXT UNIQUE NOT NULL)")
    db.conn.execute("INSERT INTO user_deposit_addresses VALUES ('test', ?)", (ADDRESS,))
    await sweep_state.ensure_sweep_schema(db)
    await sweep_state.ensure_sweep_schema(db)          # idempotent
    assert db.row()['deposit_address'] == ADDRESS
    assert db.row(2)['deposit_address'] == ''          # no address known: left as is


@pytest.mark.asyncio
async def test_ensure_sweep_schema_without_the_address_table_backfills_nothing():
    db = _RigDB()
    db.conn.execute("UPDATE crypto_payments SET deposit_address = ''")
    await sweep_state.ensure_sweep_schema(db)
    assert db.row()['deposit_address'] == ''


# -- a remote sweep waits for the credit of what sits on the address --------

def _with_mark(db, balance):
    db.conn.execute("CREATE TABLE IF NOT EXISTS deposit_balance_marks (user_id TEXT, chain TEXT, "
                    "token_symbol TEXT, balance TEXT, updated_at TEXT, as_of_block INTEGER "
                    "NOT NULL DEFAULT 0, PRIMARY KEY (user_id, chain, token_symbol))")
    if balance is not None:
        db.conn.execute("INSERT OR REPLACE INTO deposit_balance_marks (user_id, chain, token_symbol, "
                        "balance) VALUES ('test', 'ethereum', 'ETH', ?)", (str(balance),))


@pytest.mark.asyncio
@pytest.mark.parametrize('latest,mark', [(3 * 10**18, 2 * 10**18), (None, 2 * 10**18),
                                         (1 * 10**18, None)])
async def test_remote_sweep_waits_while_the_address_holds_uncredited_funds(
        monkeypatch, tmp_path, latest, mark):
    """The signer sweeps the WHOLE latest balance; the monitor credits at the
    finalized block. Latest above the credited mark (or unreadable, or no mark)
    = a deposit not yet credited: release the claim, send nothing."""
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(return_value='0x' + 'ab' * 32))
    s = _sweeper(db, wallet, latest=latest, mark=mark)
    await s._sweep_deposit(_deposit())
    wallet.sweep.assert_not_called()
    assert db.row()['sweep_state'] == 'unswept'


@pytest.mark.asyncio
async def test_remote_sweep_runs_when_the_latest_balance_is_credited(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    db = _RigDB()
    wallet = SimpleNamespace(remote=True, sweep=Mock(return_value='0x' + 'ab' * 32))
    s = _sweeper(db, wallet, latest=2 * 10**18, mark=2 * 10**18)
    await s._sweep_deposit(_deposit())
    wallet.sweep.assert_called_once()
    assert db.row()['sweep_state'] == 'swept_unproven'


# -- an unreadable store is not an empty one ---------------------------------

class _Broken(_RigDB):
    """Every read of *needle* fails the way a locked or corrupt file does."""

    def __init__(self, needle):
        super().__init__()
        self.needle = needle

    async def fetch_all(self, sql, params=()):
        if self.needle in sql:
            raise RuntimeError("database disk image is malformed")
        return await super().fetch_all(sql, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('needle', ['FROM sqlite_master', 'GROUP BY user_id',
                                    'FROM deposit_balance_marks'])
async def test_owner_report_raises_on_an_unreadable_store(needle):
    db = _Broken(needle)
    db.conn.execute("CREATE TABLE deposit_balance_marks (user_id TEXT, chain TEXT, "
                    "token_symbol TEXT, balance TEXT, as_of_block INTEGER)")
    with pytest.raises(Exception, match="malformed"):
        await sweep_state.deposit_report(db)


@pytest.mark.asyncio
async def test_owner_report_without_tables_is_empty():
    db = _RigDB()
    db.conn.execute("DROP TABLE crypto_payments")
    assert await sweep_state.deposit_report(db) == []
    db = _RigDB()                                     # no marks table yet
    rep = await sweep_state.deposit_report(db)
    assert rep and rep[0]['mark_balance'] is None


@pytest.mark.asyncio
async def test_ensure_sweep_schema_logs_an_unreadable_table(caplog):
    db = _Broken('PRAGMA table_info(crypto_payments)')
    with caplog.at_level('WARNING', logger='modules.payments.sweep_state'):
        await sweep_state.ensure_sweep_schema(db)
    assert 'sweep columns skipped' in caplog.text
