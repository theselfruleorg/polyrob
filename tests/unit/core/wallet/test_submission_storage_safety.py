"""Journal loss and signing crash windows must not reopen wallet spending."""
from pathlib import Path
import multiprocessing
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.wallet import submission_journal as journal
from core.wallet import submission_store as store
from core.wallet.broadcast.evm import EvmRail
from core.wallet.policy import PolicyGate

HASH = '0x' + 'a' * 64
ADDRESS = '0x' + 'b' * 40


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))


@pytest.mark.parametrize('damage', ['deleted_db', 'empty_db', 'deleted_marker', 'bad_marker',
                                   'deleted_row', 'changed_identity', 'invalid_identity', 'unknown_state'])
def test_lost_or_damaged_journal_refuses_new_spending(damage):
    journal.prepare(HASH, 'base', ADDRESS, 3)
    path = journal.journal_path()
    marker = Path(str(path) + '.hwm')
    if damage == 'deleted_db':
        path.unlink()
    elif damage == 'empty_db':
        path.write_bytes(b'')
    elif damage == 'deleted_marker':
        marker.unlink()
    elif damage == 'bad_marker':
        marker.write_text('{"identity": "broken", "rows": -1}')
    else:
        with sqlite3.connect(path) as db:
            if damage == 'deleted_row':
                db.execute('DELETE FROM submissions')
            elif damage == 'changed_identity':
                db.execute('UPDATE submission_identity SET identity=?', ('c' * 32,))
            elif damage == 'invalid_identity':
                db.execute('UPDATE submission_identity SET identity=?', ('bad',))
            else:
                db.execute("UPDATE submissions SET state='forgotten'")
    assert not PolicyGate(max_per_tx_usd=10).check(venue='x402', amount_usd=1, idempotency_key=None).allowed
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        journal.reserve_signing('base', ADDRESS, 4)


def test_unused_read_creates_no_database():
    assert journal.unresolved() == []
    assert not journal.journal_path().exists()


def test_intact_legacy_journal_migrates_without_losing_pending_row():
    path = journal.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE submissions (tx_hash TEXT PRIMARY KEY, chain TEXT, holder TEXT, nonce TEXT, created REAL, state TEXT)')
        db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?)', (HASH, 'base', ADDRESS, '3', 1., 'prepared'))
    assert journal.unresolved()[0]['tx_hash'] == HASH
    journal.mark_booked(HASH)
    assert Path(str(path) + '.hwm').exists()
    assert journal.unresolved() == []
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM submissions').fetchone()[0] == 1


def test_signing_error_leaves_durable_reservation_before_second_attempt(monkeypatch):
    def sign(tx):
        pending = journal.unresolved()
        assert pending[0]['state'] == 'reserved'
        assert pending[0]['nonce'] == '3'
        raise RuntimeError('signing outcome uncertain')
    signer = SimpleNamespace(address=ADDRESS, sign_transaction=Mock(side_effect=sign))
    rail = EvmRail('base', signer)
    monkeypatch.setattr(rail, 'preflight', lambda: (True, ''))
    monkeypatch.setattr(rail, '_rpc', Mock(side_effect=AssertionError('must not broadcast')))
    with pytest.raises(RuntimeError):
        rail.sign_and_send({'chainId': 8453, 'nonce': 3})
    with pytest.raises(ValueError, match='unaccounted'):
        rail.sign_and_send({'chainId': 8453, 'nonce': 3})
    assert signer.sign_transaction.call_count == 1
    assert journal.unresolved()[0]['state'] == 'reserved'


def test_marker_fsync_failure_prevents_signing(monkeypatch):
    signer = SimpleNamespace(address=ADDRESS, sign_transaction=Mock())
    rail = EvmRail('base', signer)
    monkeypatch.setattr(rail, 'preflight', lambda: (True, ''))
    monkeypatch.setattr(store, '_write_marker', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        rail.sign_and_send({'chainId': 8453, 'nonce': 3})
    signer.sign_transaction.assert_not_called()
    with pytest.raises(ValueError, match='marker missing'):
        journal.unresolved()


def test_signed_hash_binding_failure_cannot_broadcast(monkeypatch):
    signer = SimpleNamespace(address=ADDRESS, sign_transaction=Mock(return_value=b'signed-fixture'))
    rail = EvmRail('base', signer)
    monkeypatch.setattr(rail, 'preflight', lambda: (True, ''))
    rpc = Mock()
    monkeypatch.setattr(rail, '_rpc', rpc)
    monkeypatch.setattr(journal, 'bind_signed_hash', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        rail.sign_and_send({'chainId': 8453, 'nonce': 3})
    rpc.assert_not_called()
    assert journal.unresolved()[0]['state'] == 'reserved'


@pytest.mark.parametrize('amount,venue', [(4, 'x402'), (5, 'other'), (float('nan'), 'x402'), (None, 'x402')])
def test_undercharged_or_wrong_venue_attempt_stays_blocked(amount, venue):
    ref = journal.prepare_attempt('x402', ADDRESS, 5)
    with pytest.raises(ValueError, match='charge'):
        journal.mark_booked(ref, amount_usd=amount, venue=venue)
    assert journal.unresolved()[0]['tx_hash'] == ref
    journal.mark_booked(ref, amount_usd=5, venue='X402')
    assert not journal.unresolved()


def test_signing_reservation_cannot_be_booked_or_rebound():
    reference = journal.reserve_signing('base', ADDRESS, 3)
    with pytest.raises(ValueError, match='signing attempt'):
        journal.mark_booked(reference)
    journal.bind_signed_hash(reference, HASH)
    with pytest.raises(ValueError, match='missing or already bound'):
        journal.bind_signed_hash(reference, '0x' + 'c' * 64)
    assert journal.unresolved()[0]['tx_hash'] == HASH


def _reserve_in_process(data_dir, start, results, nonce):
    os.environ['POLYROB_DATA_DIR'] = data_dir
    start.wait(10)
    try:
        journal.reserve_signing('base', ADDRESS, nonce)
        results.put('reserved')
    except ValueError:
        results.put('blocked')


def test_independent_processes_cannot_both_reserve_signing(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_reserve_in_process, args=(str(tmp_path), start, results, nonce))
                 for nonce in (3, 4)]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert sorted(outcomes) == ['blocked', 'reserved']
        assert len(journal.unresolved()) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()


@pytest.mark.parametrize('target', ['database', 'marker', 'lock'])
@pytest.mark.parametrize('alias', ['symlink', 'hardlink'])
def test_journal_aliases_cannot_be_used_for_spending(tmp_path, target, alias):
    journal.prepare(HASH, 'base', ADDRESS, 3)
    path = journal.journal_path()
    target_path = {'database': path, 'marker': Path(str(path) + '.hwm'),
                   'lock': Path(str(path) + '.lock')}[target]
    other = tmp_path / 'alias'
    if alias == 'symlink':
        target_path.rename(other)
        target_path.symlink_to(other)
    else:
        os.link(target_path, other)
    with pytest.raises(ValueError, match='regular'):
        journal.unresolved()
