"""Liquidity section of the shared snapshot: chain holdings and ledger activity."""
import json


def liquidity_section(user_id, data_dir, enumerate_fn=None):
    from core.status_snapshot import Section, _rows, _telemetry_db_path
    sec = Section(name='liquidity', data={'held': None, 'moved': []})
    if enumerate_fn is None:
        sec.lines.append('held: not read (opt-in chain read; use lp_positions)')
    else:
        try:
            sec.data['held'] = list(enumerate_fn(user_id=user_id))
            sec.lines.append(f"held: {len(sec.data['held'])} position(s) from chain")
        except Exception as exc:
            sec.data['held_error'] = f'{type(exc).__name__}: {exc}'
            sec.lines.append(f"held: unavailable ({sec.data['held_error']})")
    rows = _rows(_telemetry_db_path(data_dir),
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400", (user_id,))
    unreadable = 0
    for row in rows:
        try:
            attrs = json.loads(row.get('attrs') or '{}')
            if not isinstance(attrs, dict):
                raise ValueError('attrs is not an object')
        except (ValueError, TypeError):
            unreadable += 1
            continue
        if attrs.get('action') in ('lp_add', 'lp_remove', 'lp_collect'):
            sec.data['moved'].append(dict(attrs, ts=row.get('ts')))
    sec.data['unreadable_rows'] = unreadable
    sec.lines.append(f"activity: {len(sec.data['moved'])} liquidity operation(s) in latest 400 spend rows")
    if unreadable:
        sec.lines.append(f'{unreadable} unreadable spend rows; activity may be incomplete')
    for row in sec.data['moved'][:5]:
        sec.lines.append(f"{row['action']}: {row.get('asset') or 'position unverified'} on {row.get('chain')} ({row.get('result_ref')})")
    sec.lines.append('Collect USD is the gas charge, not chain income; fee revenue is not counted as treasury income.')
    sec.lines.append('LP remaining cost basis is unavailable; liquidity operations do not adjust the token-keyed book.')
    return sec
