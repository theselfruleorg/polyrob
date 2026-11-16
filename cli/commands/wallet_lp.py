"""Owner liquidity commands; the shared dispatcher builds and guards writes.

C64: every verb below was a bare two-line body with NO docstring, so
`polyrob wallet lp --help` listed five names and said nothing about any of
them — on the one plane where "add" and "remove" move real money and "collect"
does not. Each verb now says what it does and what it costs.
"""
import click

from cli._admin_home import as_root_option


@click.group('lp')
def lp_cmd():
    """Read, create, provide, collect and withdraw Uniswap v3 liquidity; with
    --v4, read, withdraw from, collect on and move a Uniswap v4 position."""


v4_option = click.option('--v4', is_flag=True, help='A Uniswap v4 position (default: v3).')


def run(action, values, execute=False, yes=False, v4=False):
    from cli.commands.wallet import _admin_tenant, _echo_result
    from surfaces.telegram.lp_ops import dispatch
    if v4:
        values['protocol'] = 'v4'
    values = {k: v for k, v in values.items() if v is not None or k != 'token_id'}
    if action in ('add', 'remove', 'collect'):
        if execute and not yes:
            click.confirm(f'Broadcast liquidity {action} on {values["chain"]}?', abort=True)
        values['dry_run'] = not execute
    # D12: `lp_ops.dispatch` is async (a sync money helper blocked the surface
    # loop); click is sync, so bridge here — the one seat that needs it.
    from core.async_bridge import run_coroutine_sync
    # The ONE owner-tenant rule (see `wallet._owner_ctx`): a liquidity spend
    # must be recorded under the tenant this box's ledger and caps read.
    result = run_coroutine_sync(dispatch(_admin_tenant(), action, values))
    _echo_result(result, what='liquidity')


@lp_cmd.command('positions')
@click.option('--chain', required=True)
@v4_option
@click.option('--token-id', type=int, help='With --v4: one position by id.')
def positions(chain, v4, token_id):
    """Every Uniswap v3 position this wallet holds on CHAIN, or its v4 positions
    with --v4 (read-only). A v4 position by id shows its result against holding
    the deposit."""
    if token_id is not None and not v4:
        raise click.UsageError('--token-id needs --v4')
    run('positions', dict(chain=chain, token_id=token_id), v4=v4)


@lp_cmd.command('pool')
@click.argument('pool', required=False)
@click.option('--chain', required=True)
@v4_option
@click.option('--token-id', type=int, help='With --v4: the pool of this position.')
def pool(pool, chain, v4, token_id):
    """One pool's price, liquidity, fee tier and tick range (read-only). A v4
    pool has no address: name it by a position, --v4 --token-id N; the read adds
    its depth and its liquidity holders."""
    if v4:
        if pool or token_id is None:
            raise click.UsageError('a v4 pool is named by --token-id, not an address')
        return run('pool', dict(chain=chain, token_id=token_id), v4=True)
    if not pool or token_id is not None:
        raise click.UsageError('give a pool address (or --v4 --token-id N)')
    run('pool', dict(pool=pool, chain=chain))


def deposit_options(fn):
    for decorator in reversed([
        click.argument('token_a'), click.argument('token_b'),
        click.argument('amount_a', type=float), click.argument('amount_b', type=float),
        click.option('--chain', required=True), click.option('--fee', default=3000, type=int),
        click.option('--range', 'range_', default='full'), click.option('--initial-price', type=float),
    ]):
        fn = decorator(fn)
    return fn


@lp_cmd.command('quote')
@deposit_options
def quote(token_a, token_b, amount_a, amount_b, chain, fee, range_, initial_price):
    """Price a deposit without touching the chain: what the two amounts would
    buy, at what range, and what the pool would take. Signs nothing."""
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
@as_root_option
def add(token_a, token_b, amount_a, amount_b, chain, fee, range_, initial_price,
        max_usd, token_id, fragment, slippage_bps, execute, yes):
    """Provide liquidity: mint a new position, or add to --token-id.

    ⚠️ This SPENDS both tokens. --max-usd is the ceiling the guard asserts the
    simulated outflow against. Without --execute nothing is broadcast.
    """
    run('add', dict(token_a=token_a, token_b=token_b, amount_a=amount_a,
        amount_b=amount_b, chain=chain, fee=fee, range=range_, initial_price=initial_price,
        max_spend_usd=max_usd, token_id=token_id, fragment=fragment,
        slippage_bps=slippage_bps), execute, yes)


@lp_cmd.command('remove')
@click.argument('token_id', type=int)
@click.option('--chain', required=True)
@click.option('--liquidity-pct', default=100, type=int)
@click.option('--slippage-bps', default=100, type=int)
@click.option('--burn', is_flag=True)
@click.option('--max-usd', default=5.0, type=float)
@click.option('--execute', is_flag=True)
@click.option('--yes', is_flag=True)
@v4_option
@as_root_option
def remove(token_id, chain, liquidity_pct, slippage_bps, burn, max_usd, execute, yes, v4):
    """Withdraw liquidity from TOKEN_ID (default: all of it), optionally --burn
    the now-empty position NFT (v3 only). --max-usd bounds the FEE, not the
    amount returned. Without --execute nothing is broadcast."""
    if v4 and burn:
        raise click.UsageError('--burn is v3 only; an emptied v4 position stays in the wallet')
    run('remove', dict(token_id=token_id, chain=chain, liquidity_pct=liquidity_pct,
        slippage_bps=slippage_bps, burn=burn, max_spend_usd=max_usd), execute, yes, v4=v4)


@lp_cmd.command('collect')
@click.argument('token_id', type=int)
@click.option('--chain', required=True)
@click.option('--max-usd', default=5.0, type=float)
@click.option('--execute', is_flag=True)
@click.option('--yes', is_flag=True)
@v4_option
@as_root_option
def collect(token_id, chain, max_usd, execute, yes, v4):
    """Collect the fees TOKEN_ID has earned. This RECEIVES — nothing leaves the
    wallet — so --max-usd bounds the gas fee. Without --execute nothing is
    broadcast."""
    run('collect', dict(token_id=token_id, chain=chain, max_spend_usd=max_usd), execute, yes, v4=v4)


@lp_cmd.command('move')
@click.argument('token_id', type=int)
@click.option('--chain', required=True)
@click.option('--range', 'range_', required=True,
              help='full, ticks:lo,hi, or low,high as prices.')
@click.option('--liquidity-pct', default=100, type=int)
@click.option('--slippage-bps', default=100, type=int)
@as_root_option
def move(token_id, chain, range_, liquidity_pct, slippage_bps):
    """Declare the move of v4 position TOKEN_ID to a new range. This SENDS
    NOTHING: it raises the owner's confirmation card (the withdrawal, the new
    range, both minima). After Confirm, `/lp seed run <ref> max <usd> go` in
    chat withdraws and mints."""
    run('add', dict(chain=chain, protocol='v4', range=range_, move_from=token_id,
        move_pct=liquidity_pct, slippage_bps=slippage_bps, max_spend_usd=1.0, dry_run=True))
