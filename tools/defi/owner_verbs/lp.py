"""``/lp`` — the liquidity owner verb every seat runs (Telegram, the REPL and
``polyrob wallet lp``); proposal 125 S14 moved it below the seats.

⚠️ D12: ``dispatch`` and ``lp_reply`` are ``async def``. They used to hop
through ``core.async_bridge.run_coroutine_sync``, which runs the coroutine on a
PERSISTENT BACKGROUND loop and blocks the caller until it finishes — so on the
Telegram seat the whole polling loop stalled for the quote, and any loop-affine
object the rail touched (an aiohttp session, a signer client) belonged to the
other loop. A sync CLI caller bridges at ITS OWN seat; the seam is async.
"""

USAGE = '''Usage: /lp positions [on chain] | /lp positions v4 [id n]
/lp pool <pool-address> [on chain] | /lp pool v4 id <n> | /lp pool v4 <token_a> <token_b> fee <n> spacing <n> [hooks 0x…]
/lp quote <token_a> <token_b> <amount_a> <amount_b> [price n] [range low,high]
/lp add <token_a> <token_b> <amount_a> <amount_b> max <usd> [price n] [fee n] [range full|low,high|ticks:lo,hi] [id n] [frag] [go]
/lp remove <id> [pct n] [burn] [max usd] [go]
/lp collect <id> [max usd] [go]
/lp seed <token_a> <token_b> <amount_a> <amount_b> price <n> fee <n> spacing <n> [range full|ticks:lo,hi] [hooks 0x…] [gap bps]
/lp seed run <ref> max <usd> [go]
/lp move <id> range full|ticks:lo,hi|low,high [pct n] [gap bps]
/lp launch — the launch pin, the launch limits and LP_ETH_CAP
/lp launch mandate <quote token> seed <usd> count <n> days <d> [on chain] [go]
Add v4 to remove, collect or quote for a Uniswap v4 position. /lp seed declares a v4 pool, range, price and
both amounts on a card; it sends nothing. After Confirm, /lp seed run sends it once. /lp move puts a v4
position's move to a new range on the same kind of card; /lp seed run then withdraws and mints.
All support on <chain>; default robinhood. Writes simulate unless go is supplied.
Pons graduated pools pay LP fee 0; v3 creates a separate market. Removing your own liquidity removes depth for holders.'''


async def dispatch(user_id, action, values, *, seat='owner'):
    """Authenticated owner adapters supply identity; this helper never elevates it.

    W5: the verb runs between the seat stages of the one policy chain (the
    operator denylist, the effect gate and record, the refusal-taint gate and
    record — ``tools/controller/policy_chain.py``), with the one owner-seat
    context, exactly as an agent call of the same verb meets them. A refusal is
    returned as the verb's own error."""
    from core.security.turn_facts import owner_seat_context
    from tools.defi import data_tool as D, trade_tool as T
    from tools.defi.owner_verbs._seat import run_tool
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
    tool = cls()
    params = model(**values)
    ctx = owner_seat_context(user_id, seat=seat)
    return await run_tool(tool, tool_id='', verb=verb, params=params, ctx=ctx,
                          call=lambda: getattr(tool, verb)(params, ctx), as_owner=True)


def _parse_seed(values, positional):
    """117: the three ``/lp seed`` lines. A declaration and its run are the
    ``add`` verb; ``confirm`` is the owner seat's own write."""
    head = positional[0].lower() if positional else ''
    if head == 'confirm':
        if len(positional) != 2 or values.keys() - {'chain'}:
            raise ValueError('give /lp seed confirm <ref>')
        return 'seed_confirm', {'ref': positional[1]}
    if head == 'run':
        if len(positional) != 2 or values.keys() - {'chain', 'max_spend_usd', 'dry_run'}:
            raise ValueError('give /lp seed run <ref> max <usd> [go]')
        if 'max_spend_usd' not in values:
            raise ValueError('run needs max <usd> to bound both legs')
        return 'add', dict(values, protocol='v4', seed=positional[1])
    if len(positional) != 4:
        raise ValueError('give token_a token_b amount_a amount_b price <n> fee <n> spacing <n>')
    allowed = {'chain', 'fee', 'initial_price', 'range', 'tick_spacing', 'hooks', 'slippage_bps'}
    if values.keys() - allowed:
        raise ValueError(f'unsupported options for seed: {sorted(values.keys() - allowed)}')
    missing = [w for w, k in (('price', 'initial_price'), ('fee', 'fee'),
                              ('spacing', 'tick_spacing')) if k not in values]
    if missing:
        raise ValueError(f'a declaration needs {", ".join(missing)}')
    # A declaration sends nothing; the bound is given when it runs.
    return 'add', dict(values, protocol='v4', token_a=positional[0], token_b=positional[1],
                       amount_a=float(positional[2]), amount_b=float(positional[3]),
                       max_spend_usd=1.0)


def parse(args):
    action, *words = args
    if action not in ('positions', 'pool', 'quote', 'add', 'remove', 'collect', 'seed', 'move'):
        raise ValueError('unknown liquidity command')
    values, positional = {}, []
    keys = {'on': ('chain', str), 'fee': ('fee', int), 'price': ('initial_price', float),
            'range': ('range', str), 'id': ('token_id', int), 'pct': ('liquidity_pct', int),
            'max': ('max_spend_usd', float), 'slippage': ('slippage_bps', int),
            'spacing': ('tick_spacing', int), 'hooks': ('hooks', str), 'gap': ('slippage_bps', int)}
    flags = {'go': ('dry_run', False), 'burn': ('burn', True), 'frag': ('fragment', True),
             'v4': ('protocol', 'v4')}
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
    values.setdefault('chain', 'robinhood')
    if action == 'seed':
        return _parse_seed(values, positional)
    if action == 'move':
        # 118 W7: a move is a declaration too — the card, then /lp seed run.
        if len(positional) != 1 or 'range' not in values \
                or values.keys() - {'chain', 'range', 'liquidity_pct', 'slippage_bps'}:
            raise ValueError('give /lp move <id> range full|ticks:lo,hi|low,high [pct n] [gap bps]')
        return 'add', {'chain': values['chain'], 'protocol': 'v4', 'range': values['range'],
                       'move_from': int(positional[0]), 'move_pct': values.get('liquidity_pct', 100),
                       'slippage_bps': values.get('slippage_bps', 100), 'max_spend_usd': 1.0}
    v4 = values.get('protocol') == 'v4'
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
    elif action == 'pool' and v4:
        # A v4 pool has no address: the pool of a position, or the PoolKey in full.
        if len(positional) == 2:
            values.update(token_a=positional[0], token_b=positional[1])
        elif positional or 'token_id' not in values:
            raise ValueError('give /lp pool v4 id <n>, or v4 <token_a> <token_b> fee <n> spacing <n>')
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
    if v4:
        # The v4 forms of the same verbs. A v4 ADD on a named PoolKey is /lp seed.
        allowed = {
            'positions': {'chain', 'protocol', 'token_id'},
            'pool': {'chain', 'protocol', 'token_id', 'token_a', 'token_b', 'fee', 'tick_spacing', 'hooks'},
            'quote': allowed['quote'] | {'protocol', 'tick_spacing', 'hooks'},
            'add': allowed['add'] | {'protocol'},
            'remove': allowed['remove'] | {'protocol'},
            'collect': allowed['collect'] | {'protocol'},
        }
    if values.keys() - allowed[action]:
        raise ValueError(f'unsupported options for {action}: {sorted(values.keys() - allowed[action])}')
    return action, values


def seed_confirm_reply(user_id, ref):
    """117: the owner's Confirm of a pool declaration (the card runs this line).
    The ONLY writer of a CONFIRMED declaration. It sends nothing."""
    from core.wallet import lp_seed
    try:
        row = lp_seed.confirm(ref, user_id=str(user_id))
    except lp_seed.SeedUnreadable as exc:
        return f'Not confirmed: {exc}. Nothing changed.'
    except lp_seed.SeedError as exc:
        return f'Not confirmed: {exc}.'
    f = row['fields']
    # "No transaction yet", not "Nothing was sent": a card reads "Nothing was" as a
    # failure (cards._FAILURE_MARKS), and a declared move then showed as FAILED.
    return ('Declared, for ONE use within one hour. No transaction yet.\n'
            + '\n'.join(lp_seed.summary_lines(f))
            + f"\nSend it: /lp seed run {ref} max <usd> go — or tell the agent to run it. "
              f"The wallet's caps and LP_ETH_CAP still judge it.")


def launch_reply(user_id, words, *, seat='owner'):
    """122 W3: ``/lp launch`` shows the pin and the limits; ``/lp launch mandate …`` writes
    ONE mandate that covers ``defi_trade_launch_token`` with this quote token (the band is the
    seed value; the count is the launches). The pin itself is ``/config set launch.pair``."""
    from core.wallet import launch_policy as L
    from tools.defi.launch_verb import settings
    from tools.defi.lp_v4_verbs import lp_caps
    if not words:
        s = settings()
        try:
            from core.wallet.factory import get_agent_wallet
            w = get_agent_wallet()
            done = L.gate_launches_today(w.policy) if w is not None else None
        except Exception:
            done = None
        return ('Token launches (defi_trade_launch_token):\n'
                + '\n'.join(L.facts_lines(s, done_today=done, lp_caps=lp_caps()))
                + '\nSet the pin: /config set launch.pair <network>:<token>. A standing '
                  'grant: /lp launch mandate <quote token> seed <usd> count <n> days <d> go')
    if words[0] != 'mandate' or len(words) < 2:
        raise ValueError('give /lp launch, or /lp launch mandate <quote token> seed <usd> '
                         'count <n> days <d> [on chain] [go]')
    quote, rest = words[1].lower(), words[2:]
    opts, go = {}, False
    i = 0
    while i < len(rest):
        if rest[i] == 'go':
            go, i = True, i + 1
        elif rest[i] in ('seed', 'count', 'days', 'on') and i + 1 < len(rest):
            opts[rest[i]], i = rest[i + 1], i + 2
        else:
            raise ValueError(f'unknown word {rest[i]!r}')
    if {'seed', 'count', 'days'} - opts.keys():
        raise ValueError('a launch mandate names every field: seed <usd> count <n> days <d>')
    chain = opts.get('on', 'robinhood').lower()
    seed, count, days = float(opts['seed']), int(opts['count']), float(opts['days'])
    text = (f"ONE mandate: {count} launch(es) on {chain} paired to {quote}, each declaring at "
            f"most ${seed:g} (max_spend_usd), for {days:g} days. Only your standing jobs and "
            f"goals use it; the guard, your limits, launch.pair and launch.per_day still bind.")
    if not go:
        return text + '\n\nAdd `go`: /lp launch ' + ' '.join(words) + ' go'
    from core.wallet import standing_grant
    from core.wallet.token_trust import owner_seat_ctx
    try:
        L.parse_pair(f'{chain}:{quote}')
        m = standing_grant.create(owner_seat_ctx(user_id, seat), verbs=[L.ACTION],
                                  chains=[chain], assets=[quote], band_usd=(0.0, seed),
                                  count=count, total_usd=seed * count, days=days,
                                  note='/lp launch')
    except Exception as exc:
        return f'No mandate: {exc}'
    return f'Mandate {m.id}: {m.line()}'


async def lp_reply(user_id, args, *, seat='owner'):
    if not user_id:
        return 'Only the owner can use /lp.'
    if not args:
        return USAGE
    try:
        if args[0] == 'launch':
            import asyncio
            return await asyncio.to_thread(launch_reply, user_id, list(args[1:]), seat=seat)
        action, values = parse(args)
        if action == 'seed_confirm':
            return seed_confirm_reply(user_id, values['ref'])
        result = await dispatch(user_id, action, values, seat=seat)
        text = result.extracted_content
        if (not result.error and text and 'RESULT: DRY RUN' in text and values.get('dry_run', True)
                and args[0] in ('add', 'remove', 'collect', 'seed')):
            # 118 W5: the dry run is the quote; this line is what its card confirms.
            from core.surfaces import cards
            text = cards.with_go_hint(text, '\nSend it: /lp ' + ' '.join(args) + ' go')
        return result.error or text or 'No report returned; transaction state unknown. Do not retry blindly.'
    except Exception as exc:
        return f'Liquidity command error: {exc}\n{USAGE}'
