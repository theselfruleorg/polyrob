"""118 W8: `polyrob wallet lp` reaches the v4 forms of the same verbs."""
import pytest
from click.testing import CliRunner

from cli.commands import wallet_lp


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(wallet_lp, 'run',
                        lambda action, values, execute=False, yes=False, v4=False:
                        seen.append((action, values, execute, v4)))
    return seen


def invoke(*args):
    return CliRunner().invoke(wallet_lp.lp_cmd, list(args))


def test_v4_position_by_id(calls):
    assert invoke('positions', '--chain', 'c', '--v4', '--token-id', '7').exit_code == 0
    assert calls == [('positions', {'chain': 'c', 'token_id': 7}, False, True)]


def test_token_id_without_v4_is_refused(calls):
    assert invoke('positions', '--chain', 'c', '--token-id', '7').exit_code == 2
    assert calls == []


def test_v4_pool_is_named_by_a_position(calls):
    assert invoke('pool', '--chain', 'c', '--v4').exit_code == 2
    assert invoke('pool', '0xabc', '--chain', 'c', '--v4', '--token-id', '7').exit_code == 2
    assert invoke('pool', '--chain', 'c', '--v4', '--token-id', '7').exit_code == 0
    assert calls == [('pool', {'chain': 'c', 'token_id': 7}, False, True)]


def test_v4_remove_and_collect_carry_the_protocol(calls):
    assert invoke('remove', '7', '--chain', 'c', '--v4', '--liquidity-pct', '50').exit_code == 0
    assert invoke('collect', '7', '--chain', 'c', '--v4').exit_code == 0
    assert [(a, v4) for a, _, _, v4 in calls] == [('remove', True), ('collect', True)]
    assert calls[0][1]['liquidity_pct'] == 50


def test_v4_burn_is_refused(calls):
    result = invoke('remove', '7', '--chain', 'c', '--v4', '--burn')
    assert result.exit_code == 2 and 'v3 only' in result.output
    assert calls == []


def test_move_is_a_declaration_and_never_executes(calls):
    assert invoke('move', '7', '--chain', 'c', '--range', 'ticks:-200,200',
                  '--liquidity-pct', '40').exit_code == 0
    action, values, execute, _ = calls[0]
    assert action == 'add' and execute is False
    assert values['move_from'] == 7 and values['move_pct'] == 40
    assert values['protocol'] == 'v4' and values['dry_run'] is True


def test_run_sets_the_protocol_and_drops_an_unset_id(monkeypatch):
    seen = {}

    async def dispatch(user_id, action, values):
        seen.update(action=action, values=dict(values))
        return None
    monkeypatch.setattr('tools.defi.owner_verbs.lp.dispatch', dispatch)
    monkeypatch.setattr('cli.commands.wallet._admin_tenant', lambda: 'owner')
    monkeypatch.setattr('cli.commands.wallet._echo_result', lambda result, what: None)
    wallet_lp.run('positions', {'chain': 'c', 'token_id': None}, v4=True)
    assert seen == {'action': 'positions', 'values': {'chain': 'c', 'protocol': 'v4'}}
