import pytest


@pytest.fixture(autouse=True)
def isolated_x_write_budget(tmp_path, monkeypatch):
    from polyrob_x import write_budget
    monkeypatch.setattr(write_budget, '_db_path', lambda: tmp_path / 'x_write_attempts.db')
