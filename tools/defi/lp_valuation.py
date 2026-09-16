"""Display-only position valuation; unknown prices/cost records stay unknown."""
import json
from math import isfinite


def value_lines(tool, chain, pv, context):
    values, fees = [], []
    for token, amount, fee, decimals in (
            (pv.token0, pv.amount0, pv.fees0, pv.dec0),
            (pv.token1, pv.amount1, pv.fees1, pv.dec1)):
        try:
            price = tool._price_for(chain, token).price_usd
            if price is None or not isfinite(price) or price <= 0:
                raise ValueError('no price')
            values.append(amount / 10 ** decimals * price)
            fees.append(fee / 10 ** decimals * price)
        except Exception:
            values.append(None if amount else 0)
            fees.append(None if fee else 0)
    def usd(parts):
        return 'unknown (missing price)' if None in parts else f'${sum(parts):.8g}'
    lines = [f'  estimated position value: {usd(values)}; accrued fees: {usd(fees)}']
    try:
        from core.instance import resolve_owner_user_id
        from core.runtime_paths import data_dir_or_home
        from core.status_snapshot import _rows, _telemetry_db_path
        from core.wallet.dex_registry import resolve_position_manager
        uid = getattr(context, 'user_id', None) or resolve_owner_user_id()
        asset = f'erc721:{resolve_position_manager(chain, "v3").lower()}:{pv.token_id}'
        rows = _rows(_telemetry_db_path(data_dir_or_home(None)),
            "SELECT attrs FROM telemetry_events WHERE kind='wallet_spend' AND user_id=?", (uid,))
        deposits = []
        for row in rows:
            attrs = json.loads(row['attrs'])
            if (attrs.get('chain') == chain and str(attrs.get('asset', '')).lower() == asset
                    and attrs.get('action') == 'lp_add'):
                deposits.append(float(attrs['amount_usd']))
        lines.append('  cumulative deposited USD (ledger, before withdrawals): ' +
                     (f'${sum(deposits):.8g}' if deposits else 'unknown (no recorded entry)'))
    except Exception as exc:
        lines.append(f'  entry ledger: unavailable ({type(exc).__name__})')
    return lines
