"""3.11 (O5, 2026-07-14 review) — a pairing code must be approvable from the CLI.

core/pairing.py documented `rob pair approve <code>` but no such command existed
anywhere: with POLYROB_REQUIRE_PAIRING=true a stranger was issued a code no CLI
could approve. `polyrob owner pair {pending,approve,revoke}` is the thin wrapper
over the same PairingStore (same data-home resolution as the surface daemons).
"""
import os

import pytest
from click.testing import CliRunner

from core.pairing import PairingStore


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _store(data_home) -> PairingStore:
    return PairingStore(os.path.join(str(data_home), "pairing.db"))


def test_pair_pending_lists_codes(data_home):
    from cli.commands.owner import owner
    code = _store(data_home).request("tg:12345")
    res = CliRunner().invoke(owner, ["pair", "pending"])
    assert res.exit_code == 0
    assert "tg:12345" in res.output and code in res.output


def test_pair_approve_pairs_the_user(data_home):
    from cli.commands.owner import owner
    store = _store(data_home)
    code = store.request("tg:12345")
    res = CliRunner().invoke(owner, ["pair", "approve", code])
    assert res.exit_code == 0
    assert "tg:12345" in res.output
    assert store.is_paired("tg:12345") is True


def test_pair_approve_unknown_code_fails(data_home):
    from cli.commands.owner import owner
    res = CliRunner().invoke(owner, ["pair", "approve", "deadbeef"])
    assert res.exit_code != 0
    assert "no pending" in res.output.lower()


def test_pair_revoke_unpairs(data_home):
    from cli.commands.owner import owner
    store = _store(data_home)
    code = store.request("tg:12345")
    store.approve(code)
    assert store.is_paired("tg:12345")
    res = CliRunner().invoke(owner, ["pair", "revoke", "tg:12345"])
    assert res.exit_code == 0
    assert store.is_paired("tg:12345") is False


def test_pairing_docstring_matches_real_command():
    """The module doc must name the command that actually exists."""
    import core.pairing as p
    assert "rob pair approve" not in (p.__doc__ or "")
    assert "polyrob owner pair approve" in (p.__doc__ or "")
