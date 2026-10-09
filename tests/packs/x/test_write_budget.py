from concurrent.futures import ThreadPoolExecutor

from polyrob_x import write_budget


def test_concurrent_tools_share_one_durable_budget(monkeypatch):
    monkeypatch.setenv('TWITTER_WRITE_MAX_PER_HOUR', '3')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: write_budget.reserve_write(), range(12)))
    assert sum(results) == 3
    assert not write_budget.reserve_write()


def test_api_browser_and_dm_share_aggregate_budget(monkeypatch):
    monkeypatch.setenv('TWITTER_WRITE_MAX_PER_HOUR', '2')
    monkeypatch.setenv('TWITTER_DM_MAX_PER_HOUR', '1')
    assert write_budget.reserve_write(is_dm=True)
    assert not write_budget.reserve_write(is_dm=True)
    assert write_budget.reserve_write()
    assert not write_budget.reserve_write()


def test_thread_reservation_is_atomic(monkeypatch):
    monkeypatch.setenv('TWITTER_WRITE_MAX_PER_HOUR', '3')
    assert not write_budget.reserve_write(units=4)
    assert write_budget.reserve_write(units=3)
    assert not write_budget.reserve_write()


def test_unreadable_budget_refuses(monkeypatch, tmp_path):
    path = tmp_path / 'broken.db'
    path.write_text('not sqlite')
    monkeypatch.setattr(write_budget, '_db_path', lambda: path)
    assert not write_budget.reserve_write()
