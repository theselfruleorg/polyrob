"""Owner-login limits survive restarts, address churn and concurrent workers."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from core.rate_limit import PersistentWindowLimiter
from webview import login_limits


def test_ipv6_aliases_and_rotation_share_a_budget():
    for n in range(5):
        assert login_limits.admit(f'2001:db8:1:2::{n + 1}')
    assert not login_limits.admit('2001:0db8:0001:0002:ffff::1')
    assert login_limits.admit('2001:db8:1:3::1')


def test_ipv4_mapped_address_cannot_double_the_budget():
    for _ in range(5):
        assert login_limits.admit('192.0.2.1')
    assert not login_limits.admit('::ffff:192.0.2.1')


def test_distributed_attempts_are_bounded_and_do_not_grow_the_store():
    for n in range(50):
        assert login_limits.admit(f'192.0.2.{n}')
    for n in range(200):
        assert not login_limits.admit(f'198.51.100.{n}')
    with sqlite3.connect(login_limits._db_path()) as db:
        assert db.execute('SELECT count(*) FROM rate_events').fetchone()[0] == 100


def test_new_instances_and_concurrent_workers_share_one_budget():
    def attempt(_):
        return login_limits.admit('192.0.2.1')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert sum(results) == 5
    assert not login_limits.admit('192.0.2.1')


def test_window_expiry_releases_only_expired_attempts(tmp_path):
    clock = [1000]
    limiter = PersistentWindowLimiter(tmp_path / 'rate.db', time_fn=lambda: clock[0])
    assert limiter.check_many({'owner': 1})
    clock[0] += 299
    assert not limiter.check_many({'owner': 1})
    clock[0] += 1
    assert limiter.check_many({'owner': 1})


def test_unreadable_store_does_not_grant_a_fresh_budget():
    login_limits._db_path().write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        login_limits.admit('192.0.2.1')
