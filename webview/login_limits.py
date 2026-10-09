"""Owner-login budgets shared by workers; remote address churn buys no reset.

Two budgets per window: 5 tries per address bucket (an IPv6 /64 is one
bucket) and 50 tries across all addresses. The global budget alone would let
about ten /64s lock the owner out of the console, so an address bucket that
signed in successfully in the last :data:`TRUSTED_TTL_SECONDS` is held only to
its own budget: a flood from other addresses never refuses the owner's
known address a try.
"""
import asyncio
import ipaddress
import logging
import time

from core.rate_limit import PersistentWindowLimiter
from core.runtime_paths import data_home_db_path

logger = logging.getLogger(__name__)

#: How long a successful sign-in keeps its address bucket out of the global budget.
TRUSTED_TTL_SECONDS = 30 * 24 * 3600

_TRUSTED_SCHEMA = ('CREATE TABLE IF NOT EXISTS trusted_buckets '
                   '(bucket TEXT PRIMARY KEY, ts REAL)')


def _db_path():
    return data_home_db_path('login_attempts.db')


def address_bucket(ip):
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return 'unknown'
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f'{addr}/64', strict=False))
    return str(addr)


def _trusted(bucket, now=None):
    if bucket == 'unknown':
        return False
    from core.sqlite_util import init_schema, wal_connect
    path = str(_db_path())
    init_schema(path, _TRUSTED_SCHEMA, mkdir=True)
    db = wal_connect(path)
    try:
        row = db.execute('SELECT ts FROM trusted_buckets WHERE bucket=?',
                         (bucket,)).fetchone()
    finally:
        db.close()
    now = time.time() if now is None else now
    return bool(row) and (now - float(row[0])) < TRUSTED_TTL_SECONDS


def admit(ip):
    bucket = address_bucket(ip)
    limits = {'address:' + bucket: 5}
    if not _trusted(bucket):
        limits['owner-login'] = 50
    return PersistentWindowLimiter(_db_path(), window_seconds=300).check_many(limits)


def record_success(ip):
    """Remember that *ip*'s bucket signed in (keeps it out of the global budget)."""
    bucket = address_bucket(ip)
    if bucket == 'unknown':
        return
    from core.sqlite_util import init_schema, wal_connect
    path = str(_db_path())
    init_schema(path, _TRUSTED_SCHEMA, mkdir=True)
    db = wal_connect(path)
    try:
        with db:
            db.execute('INSERT OR REPLACE INTO trusted_buckets VALUES (?, ?)',
                       (bucket, time.time()))
            db.execute('DELETE FROM trusted_buckets WHERE ts <= ?',
                       (time.time() - TRUSTED_TTL_SECONDS,))
    finally:
        db.close()


async def remember(ip):
    """:func:`record_success` off the event loop. Fail-open: a store fault
    costs only the global-budget exemption, never the sign-in."""
    try:
        await asyncio.to_thread(record_success, ip)
    except Exception:
        logger.warning("owner login: could not remember the address", exc_info=True)
