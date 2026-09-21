"""Shared owner seat for liquidity; CLI and REPL call the same dispatcher.

⚠️ D12: ``dispatch`` and ``lp_reply`` are ``async def``. They used to hop
through ``core.async_bridge.run_coroutine_sync``, which runs the coroutine on a
PERSISTENT BACKGROUND loop and blocks the caller until it finishes — so on the
Telegram seat the whole polling loop stalled for the quote, and any loop-affine
object the rail touched (an aiohttp session, a signer client) belonged to the
other loop. A sync CLI caller bridges at ITS OWN seat; the seam is async.
"""

USAGE = '''Usage: /lp positions [on chain]
/lp pool <pool-address> [on chain]
/lp quote <token_a> <token_b> <amount_a> <amount_b> [price n] [range low,high]
/lp add <token_a> <token_b> <amount_a> <amount_b> max <usd> [price n] [fee n] [range full|low,high|ticks:lo,hi] [id n] [frag] [go]
/lp remove <id> [pct n] [burn] [max usd] [go]
/lp collect <id> [max usd] [go]
All support on <chain>; default robinhood. Writes simulate unless go is supplied.
Pons graduated pools pay LP fee 0; v3 creates a separate market. Removing your own liquidity removes depth for holders.'''


async def dispatch(user_id, action, values):
    """Authenticated owner adapters supply identity; this helper never elevates it."""
    from surfaces.telegram.token_ops import _owner_ctx
    from tools.defi import data_tool as D, trade_tool as T
    mapping = {
        'positions': (D.DefiDataTool, 'lp_positions', D.LpPositionsParams),
        'pool': (D.DefiDataTool, 'lp_pool_info', D.LpPoolInfoParams),
        'quote': (D.DefiDataTool, 'lp_quote', D.LpQuoteParams),
        'add': (T.DefiTradeTool, 'lp_add', T.LpAddParams),
        'remove': (T.DefiTradeTool, 'lp_remove', T.LpRemoveParams),
        'collect': (T.DefiTradeTool, 'lp_collect', T.LpCollectParams),
    }
    if not user_id:
        raise ValueError('Only the owner can use /lp.')
    cls, verb, model = mapping[action]
    from core.exec_identity import set_exec_identity, reset_exec_identity
    token = set_exec_identity(user_id, None)
    try:
        return await getattr(cls(), verb)(model(**values), _owner_ctx(user_id))
    finally:
        reset_exec_identity(token)


def parse(args):
    action, *words = args
    if action not in ('positions', 'pool', 'quote', 'add', 'remove', 'collect'):
        raise ValueError('unknown liquidity command')
    values, positional = {}, []
    keys = {'on': ('chain', str), 'fee': ('fee', int), 'price': ('initial_price', float),
            'range': ('range', str), 'id': ('token_id', int), 'pct': ('liquidity_pct', int),
            'max': ('max_spend_usd', float), 'slippage': ('slippage_bps', int)}
    flags = {'go': ('dry_run', False), 'burn': ('burn', True), 'frag': ('fragment', True)}
    i = 0
    while i < len(words):
        word = words[i]
        if word in keys:
            key, convert = keys[word]
            if i + 1 >= len(words):
                raise ValueError(f'{word} needs a value')
            values[key] = convert(words[i + 1])
            i += 2
        elif word in flags:
            key, value = flags[word]
            values[key] = value
            i += 1
        else:
            positional.append(word)
            i += 1
    if action in ('add', 'quote'):
        if len(positional) != 4:
            raise ValueError('give token_a token_b amount_a amount_b')
        values.update(token_a=positional[0], token_b=positional[1],
                      amount_a=float(positional[2]), amount_b=float(positional[3]))
        if action == 'add' and 'max_spend_usd' not in values:
            raise ValueError('add needs max <usd> to bound both legs')
    elif action in ('remove', 'collect'):
        if len(positional) != 1:
            raise ValueError('give the position id')
        values['token_id'] = int(positional[0])
    elif action == 'pool':
        if len(positional) != 1:
            raise ValueError('give a pool address')
        values['pool'] = positional[0]
    elif positional:
        raise ValueError('positions takes only on <chain>')
    # Pydantic normally ignores extra fields; command typos must not silently
    # discard a price, burn request or execution switch.
    allowed = {
        'positions': {'chain'}, 'pool': {'chain', 'pool'},
        'quote': {'chain', 'token_a', 'token_b', 'amount_a', 'amount_b', 'fee', 'initial_price', 'range'},
        'add': {'chain', 'token_a', 'token_b', 'amount_a', 'amount_b', 'fee', 'initial_price', 'range', 'token_id', 'fragment', 'dry_run', 'max_spend_usd', 'slippage_bps'},
        'remove': {'chain', 'token_id', 'liquidity_pct', 'burn', 'dry_run', 'max_spend_usd', 'slippage_bps'},
        'collect': {'chain', 'token_id', 'dry_run', 'max_spend_usd'},
    }
    if values.keys() - allowed[action]:
        raise ValueError(f'unsupported options for {action}: {sorted(values.keys() - allowed[action])}')
    return action, values


async def lp_reply(user_id, args):
    if not user_id:
        return 'Only the owner can use /lp.'
    if not args:
        return USAGE
    try:
        action, values = parse(args)
        result = await dispatch(user_id, action, values)
        return result.error or result.extracted_content or 'No report returned; transaction state unknown. Do not retry blindly.'
    except Exception as exc:
        return f'Liquidity command error: {exc}\n{USAGE}'
