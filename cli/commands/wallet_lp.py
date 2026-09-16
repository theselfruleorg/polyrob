"""Owner liquidity commands; the shared dispatcher builds and guards writes."""
import click


@click.group('lp')
def lp_cmd():
    """Read, create, provide, collect and withdraw Uniswap v3 liquidity."""


def run(action, values, execute=False, yes=False):
    from cli.commands.wallet import _echo_result
    from core.instance import resolve_owner_user_id
    from surfaces.telegram.lp_ops import dispatch
    if action in ('add', 'remove', 'collect'):
        if execute and not yes:
            click.confirm(f'Broadcast liquidity {action} on {values["chain"]}?', abort=True)
        values['dry_run'] = not execute
    result = dispatch(resolve_owner_user_id(), action, values)
    _echo_result(result, what='liquidity')


@lp_cmd.command('positions')
@click.option('--chain', default='robinhood')
def positions(chain):
    run('positions', dict(chain=chain))


@lp_cmd.command('pool')
@click.argument('pool')
@click.option('--chain', default='robinhood')
def pool(pool, chain):
    run('pool', dict(pool=pool, chain=chain))


def deposit_options(fn):
    for decorator in reversed([
        click.argument('token_a'), click.argument('token_b'),
        click.argument('amount_a', type=float), click.argument('amount_b', type=float),
        click.option('--chain', default='robinhood'), click.option('--fee', default=3000, type=int),
        click.option('--range', 'range_', default='full'), click.option('--initial-price', type=float),
    ]):
        fn = decorator(fn)
    return fn


@lp_cmd.command('quote')
@deposit_options
def quote(token_a, token_b, amount_a, amount_b, chain, fee, range_, initial_price):
    run('quote', dict(token_a=token_a, token_b=token_b, amount_a=amount_a,
        amount_b=amount_b, chain=chain, fee=fee, range=range_, initial_price=initial_price))


@lp_cmd.command('add')
@deposit_options
@click.option('--max-usd', required=True, type=float)
@click.option('--token-id', type=int)
@click.option('--fragment', is_flag=True)
@click.option('--slippage-bps', default=100, type=int)
@click.option('--execute', is_flag=True, help='Broadcast; default is a dry run.')
@click.option('--yes', is_flag=True)
def add(token_a, token_b, amount_a, amount_b, chain, fee, range_, initial_price,
        max_usd, token_id, fragment, slippage_bps, execute, yes):
    run('add', dict(token_a=token_a, token_b=token_b, amount_a=amount_a,
        amount_b=amount_b, chain=chain, fee=fee, range=range_, initial_price=initial_price,
        max_spend_usd=max_usd, token_id=token_id, fragment=fragment,
        slippage_bps=slippage_bps), execute, yes)


@lp_cmd.command('remove')
@click.argument('token_id', type=int)
@click.option('--chain', default='robinhood')
@click.option('--liquidity-pct', default=100, type=int)
@click.option('--slippage-bps', default=100, type=int)
@click.option('--burn', is_flag=True)
@click.option('--max-usd', default=5.0, type=float)
@click.option('--execute', is_flag=True)
@click.option('--yes', is_flag=True)
def remove(token_id, chain, liquidity_pct, slippage_bps, burn, max_usd, execute, yes):
    run('remove', dict(token_id=token_id, chain=chain, liquidity_pct=liquidity_pct,
        slippage_bps=slippage_bps, burn=burn, max_spend_usd=max_usd), execute, yes)


@lp_cmd.command('collect')
@click.argument('token_id', type=int)
@click.option('--chain', default='robinhood')
@click.option('--max-usd', default=5.0, type=float)
@click.option('--execute', is_flag=True)
@click.option('--yes', is_flag=True)
def collect(token_id, chain, max_usd, execute, yes):
    run('collect', dict(token_id=token_id, chain=chain, max_spend_usd=max_usd), execute, yes)
