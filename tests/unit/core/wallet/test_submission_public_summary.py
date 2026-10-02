"""066 P1 — the console reads the submission journal again, WITHOUT the journal
being readable by it.

066 P0.4 pinned ``submissions.sqlite`` and its marker 0600 to polyrob-agent; the
console (polyrob-web) then showed "submission journal unavailable" for every
wallet view. The agent now writes ``submissions.public.json`` (0640) on every
journal write, and a reader that cannot open the journal reads that — refusing
it when the journal changed after it was written. The "other identity" is
simulated by removing every permission from the journal files (the test is not
root, so the open fails exactly as it does for polyrob-web).
"""
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.wallet import submission_journal as journal
from core.wallet import submission_store as store

HASH = '0x' + 'c' * 64
ADDRESS = '0x' + 'd' * 40

pytestmark = pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() == 0,
                                reason='root ignores file modes')


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))


def _journal_files():
    path = journal.journal_path()
    return [p for p in (path, Path(str(path) + '.hwm'), Path(str(path) + '.lock')) if p.exists()]


class _AsWebIdentity:
    """Every journal file unreadable, as it is for polyrob-web after P0.4."""

    def __enter__(self):
        self.modes = {p: stat.S_IMODE(p.stat().st_mode) for p in _journal_files()}
        for p in self.modes:
            os.chmod(p, 0)
        return self

    def __exit__(self, *exc):
        for p, m in self.modes.items():
            os.chmod(p, m)


def test_every_write_leaves_a_group_readable_public_summary():
    journal.prepare(HASH, 'base', ADDRESS, 7)
    summary = store.public_summary_path(journal.journal_path())
    assert summary.is_file()
    assert stat.S_IMODE(summary.stat().st_mode) == 0o640
    assert not summary.name.startswith('submissions.sqlite')   # outside the deploy's 0600 glob


def test_the_console_identity_reads_the_unresolved_rows_through_the_summary():
    journal.prepare(HASH, 'base', ADDRESS, 7)
    as_agent = journal.unresolved()
    with _AsWebIdentity():
        with pytest.raises(PermissionError):
            with store.connection(journal.journal_path()):
                pass                                  # the journal itself stays closed to it
        as_web = journal.unresolved()
    assert as_web == as_agent and as_web[0]['tx_hash'] == HASH and as_web[0]['state'] == 'prepared'


def test_a_booked_row_disappears_from_the_summary_too():
    journal.prepare(HASH, 'base', ADDRESS, 7)
    journal.mark_booked(HASH)
    with _AsWebIdentity():
        assert journal.unresolved() == []


def test_a_journal_changed_after_its_summary_is_unavailable_not_clean():
    """A crash between the commit and the summary write: the console must say
    "unavailable", never report the stale (possibly empty) list."""
    journal.prepare(HASH, 'base', ADDRESS, 7)
    journal.mark_booked(HASH)
    path = journal.journal_path()
    summary = store.public_summary_path(path)
    frozen = summary.read_bytes()
    journal.prepare('0x' + 'e' * 64, 'base', ADDRESS, 8)      # a new unresolved row …
    summary.write_bytes(frozen)                              # … and the summary lost it
    with _AsWebIdentity():
        with pytest.raises(store.JournalUnavailable, match='changed after its public summary'):
            journal.unresolved()


def test_no_summary_yet_is_unavailable():
    journal.prepare(HASH, 'base', ADDRESS, 7)
    store.public_summary_path(journal.journal_path()).unlink()
    with _AsWebIdentity():
        with pytest.raises(store.JournalUnavailable, match='no public summary yet'):
            journal.unresolved()


def test_refresh_writes_a_summary_for_an_existing_journal_and_never_creates_one(tmp_path):
    missing = tmp_path / 'wallet' / 'submissions.sqlite'
    assert store.refresh_public_summary(missing) is False
    assert not missing.exists()
    journal.prepare(HASH, 'base', ADDRESS, 7)
    summary = store.public_summary_path(journal.journal_path())
    summary.unlink()
    assert store.refresh_public_summary(journal.journal_path()) is True
    with _AsWebIdentity():
        assert journal.unresolved()[0]['tx_hash'] == HASH


def test_the_wallet_view_shows_the_journal_to_the_console(monkeypatch):
    """The regression itself: the view the console renders carries the rows and
    no "submission journal unavailable" error."""
    from core.wallet import view as wallet_view_mod
    monkeypatch.setattr(wallet_view_mod, 'owner_refusal', lambda uid: None)
    wallet = SimpleNamespace(
        signing_available=False, network='base', operational_venue='treasury',
        address_for_venue=lambda venue: ADDRESS, solana_address='So1ana',
        policy=SimpleNamespace(per_tx_cap_usd=5.0, daily_cap_usd=20.0))
    journal.prepare(HASH, 'base', ADDRESS, 7)
    with _AsWebIdentity():
        v = wallet_view_mod.wallet_view('owner', wallet_fn=lambda: wallet)
    assert not [e for e in v.errors if 'submission journal' in e], v.errors
    assert [r['tx_hash'] for r in v.unaccounted_submissions] == [HASH]
