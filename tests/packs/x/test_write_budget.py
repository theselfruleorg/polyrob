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


def test_refused_budget_names_when_the_cap_reopens(monkeypatch):
    monkeypatch.setenv('TWITTER_WRITE_MAX_PER_HOUR', '9')
    monkeypatch.setenv('TWITTER_DM_MAX_PER_HOUR', '1')
    assert write_budget.refused_budget(is_dm=True) == ('DMs', 0)   # nothing is full
    assert write_budget.reserve_write(is_dm=True)
    kind, wait = write_budget.refused_budget(is_dm=True)
    assert kind == 'DMs' and 3500 < wait <= 3600
    assert write_budget.refused_budget() == ('writes', 0)          # the shared cap is not full


def test_a_dm_refused_by_the_shared_cap_names_the_shared_cap(monkeypatch):
    """A DM checks both buckets: when the shared writes cap refused it, the answer is the
    shared cap and its reopening, never the DM bucket that still has room."""
    monkeypatch.setenv('TWITTER_WRITE_MAX_PER_HOUR', '2')
    monkeypatch.setenv('TWITTER_DM_MAX_PER_HOUR', '5')
    assert write_budget.reserve_write() and write_budget.reserve_write()
    assert not write_budget.reserve_write(is_dm=True)
    kind, wait = write_budget.refused_budget(is_dm=True)
    assert kind == 'writes' and 3500 < wait <= 3600


def test_refused_budget_is_zero_when_unreadable(monkeypatch, tmp_path):
    path = tmp_path / 'broken.db'
    path.write_text('not sqlite')
    monkeypatch.setattr(write_budget, '_db_path', lambda: path)
    assert write_budget.refused_budget(is_dm=True) == ('DMs', 0)
