"""Observable fungible receipt assertions; native transfers require traces.

ERC20 Transfer logs supplement simulation balance reads. They cannot prove
native refunds or balance changes of nonstandard tokens without transfer logs.
"""
from core.wallet import simulation
from core.wallet.liquidity_guard import legs


def assert_fungible_receipt(intent, logs, holder):
    outgoing, incoming = legs(intent.lp_outflows), legs(intent.lp_inflows)
    net = {}
    holder_word = holder.lower()[2:].rjust(64, '0')
    for log in logs:
        topics = [str(t).lower() for t in log.get('topics', [])]
        if len(topics) != 3 or topics[0] != simulation._TOPIC_TRANSFER:
            continue
        sent, received = topics[1][2:] == holder_word, topics[2][2:] == holder_word
        if not (sent or received):
            continue
        token = str(log.get('address', '')).lower()
        data = log.get('data', '')
        if len(data) != 66:
            raise ValueError('malformed fungible receipt transfer')
        amount = int(data, 16)
        if token not in outgoing and token not in incoming and amount:
            raise ValueError('receipt contains an undeclared fungible transfer')
        net[token] = net.get(token, 0) + amount * (int(received) - int(sent))
    for token, maximum in outgoing.items():
        if token is not None and not 0 < -net.get(token, 0) <= maximum:
            raise ValueError('receipt fungible outflow missing or exceeds maximum')
    for token, minimum in incoming.items():
        if token is not None and net.get(token, 0) < minimum:
            raise ValueError('receipt fungible inflow below minimum')
    events = simulation._holder_events(logs, holder)
    if events.operator_grants or events.approvals:
        raise ValueError('receipt contains an undeclared approval')
    npm, token_id = intent.lp_position
    for contract, spender, approved_id in events.nft_approvals:
        # Standard NPM burn clears the approval before destroying the NFT.
        if not (intent.lp_position_effect == 'burn' and contract == npm.lower()
                and approved_id == token_id and spender == '0x' + '0' * 40):
            raise ValueError('receipt contains an undeclared NFT approval')
