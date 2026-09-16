from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from surfaces.telegram import lp_ops as L
from cli.commands.wallet_lp import lp_cmd


def test_owner_and_usage():
    assert 'Only the owner' in L.lp_reply(None, [])
    assert all(v in L.lp_reply('owner', []) for v in ('positions', 'quote', 'add', 'remove', 'collect'))


def test_parse_dry_run_and_explicit_go():
    action, values = L.parse(['add', 'native', '0xabc', '0.001', '1000', 'max', '5', 'fee', '3000', 'go'])
    assert action == 'add' and values['dry_run'] is False and values['max_spend_usd'] == 5
    with pytest.raises(ValueError, match='max'):
        L.parse(['add', 'native', '0xabc', '1', '1'])
    with pytest.raises(ValueError, match='unsupported'):
        L.parse(['collect', '42', 'burn'])


def test_positions_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr(L, 'dispatch', lambda uid, action, values:
        seen.append((uid, action, values)) or SimpleNamespace(error=None, extracted_content='positions'))
    assert L.lp_reply('owner', ['positions', 'on', 'base']) == 'positions'
    assert seen == [('owner', 'positions', {'chain': 'base'})]


def test_cli_help_and_no_implicit_broadcast(monkeypatch):
    runner = CliRunner()
    assert '--execute' in runner.invoke(lp_cmd, ['add', '--help']).output
    seen = []
    monkeypatch.setattr(L, 'dispatch', lambda uid, action, values:
        seen.append((uid, action, values)) or SimpleNamespace(error=None, extracted_content='dry run'))
    result = runner.invoke(lp_cmd, ['collect', '42'])
    assert result.exit_code == 0, result.output
    assert seen[0][0] and seen[0][2]['dry_run'] is True
