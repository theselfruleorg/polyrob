"""Shared owner-only public wallet view. No RPC, raw accounts or secret fields."""
from dataclasses import asdict, dataclass, field

from core.wallet.authority import owner_refusal


@dataclass
class AccountView:
    role: str
    family: str
    address: str | None
    receive: bool
    error: str | None = None


@dataclass
class WalletView:
    owner: str
    state: str = 'unavailable'
    signing_available: bool = False
    network: str | None = None
    accounts: list = field(default_factory=list)
    caps: dict = field(default_factory=dict)
    balances: dict = field(default_factory=lambda: {'state': 'unread', 'chains': []})
    unaccounted_submissions: list | None = None
    errors: list = field(default_factory=list)


def wallet_view(user_id, *, data_dir=None, wallet_fn=None):
    error = owner_refusal(user_id)
    if error:
        raise PermissionError(error)
    result = WalletView(owner=user_id)
    if wallet_fn is None:
        from core.wallet.factory import get_agent_wallet
        wallet_fn = get_agent_wallet
    try:
        wallet = wallet_fn()
        if wallet is None:
            result.state = 'disabled'
            return result
        result.signing_available = wallet.signing_available is True
        result.network = wallet.network
        result.state = 'ready' if result.signing_available else 'public_only'
        for venue in ('treasury', 'x402', 'hyperliquid', 'polymarket'):
            try:
                address = wallet.address_for_venue(venue)
                role = 'operational' if venue == wallet.operational_venue else venue
                result.accounts.append(AccountView(role, 'evm', address,
                                                    venue in ('treasury', 'x402')))
            except Exception:
                result.accounts.append(AccountView(venue, 'evm', None, False, 'address unavailable'))
        try:
            result.accounts.append(AccountView('account0', 'solana', wallet.solana_address, True))
        except Exception:
            result.accounts.append(AccountView('account0', 'solana', None, False, 'address unavailable'))
        gate = wallet.policy
        result.caps = {'per_tx_usd': gate.per_tx_cap_usd, 'daily_usd': gate.daily_cap_usd}
    except Exception:
        result.errors.append('wallet identity or policy unavailable')
        result.state = 'unavailable'
        return result
    from core.wallet import balance_cache, submission_journal
    cached = balance_cache.read(data_dir)
    if cached is not None:
        evm = next((a.address for a in result.accounts if a.role == 'operational'), None)
        sol = next((a.address for a in result.accounts if a.family == 'solana'), None)
        # Cache belongs to a wallet identity, not merely a data directory.
        if cached.address != evm or cached.solana_address != sol:
            result.errors.append('balance cache identity mismatch; balances withheld')
        else:
            result.balances = {'state': 'stale' if cached.stale else 'cached',
                'observed_at': cached.taken_at, 'age_sec': cached.age_sec,
                'chains': [asdict(row) for row in cached.chains]}
    try:
        result.unaccounted_submissions = submission_journal.unresolved()
    except Exception:
        result.errors.append('submission journal unavailable; spending must remain blocked')
    return result


def render_wallet(view):
    lines = [f'Wallet: {view.state}', f'Network: {view.network or "unavailable"}',
             'Signing in this process: ' + ('available' if view.signing_available else 'unavailable')]
    for account in view.accounts:
        suffix = 'receive' if account.receive else 'signing identity only — do not fund'
        lines.append(f'{account.family} / {account.role}: {account.address or "unavailable"} ({suffix})')
    if view.caps:
        daily = view.caps['daily_usd']
        lines.append(f'Caps: ${view.caps["per_tx_usd"]:g}/tx; daily ' +
                     ('unlimited' if daily is None else f'${daily:g}'))
    lines.append(f'Balances: {view.balances["state"]}')
    for row in view.balances['chains']:
        native, usdc = row.get('native'), row.get('usdc')
        lines.append(f'{row["chain"]}: {native if native is not None else "unavailable"} '
                     f'{row["symbol"]}; USDC {usdc if usdc is not None else "unavailable"}')
    if view.unaccounted_submissions:
        lines.append('Spending blocked by unaccounted submissions:')
        lines.extend(f'{r["chain"]}: {r["tx_hash"]}' for r in view.unaccounted_submissions)
    lines.extend(view.errors)
    return '\n'.join(lines)
