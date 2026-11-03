"""098 CO-11: the credit latch is one locked, atomic file."""
import asyncio
import json
import multiprocessing
import os
import time

import pytest


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")
    return tmp_path


def _trip(key):
    from core.credit_sentinel import trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("402", provider=key, container=None, user_id="u"))


def _reader(path, stop_at, q):
    bad = 0
    while time.time() < stop_at:
        try:
            with open(path) as fh:
                txt = fh.read()
            if txt:
                json.loads(txt)
        except FileNotFoundError:
            pass
        except ValueError:
            bad += 1
    q.put(bad)


def test_writer_is_atomic(data_dir):
    from core.credit_sentinel import _sentinel_path, _write_latch
    path = _sentinel_path()
    _write_latch(path, {"x": {"ts": time.time()}})
    q = multiprocessing.get_context("spawn").Queue()
    p = multiprocessing.get_context("spawn").Process(
        target=_reader, args=(path, time.time() + 1.5, q))
    p.start()
    end = time.time() + 1.5
    i = 0
    while time.time() < end:
        _write_latch(path, {f"p{j}": {"ts": time.time(), "reason": "r" * 200}
                            for j in range(i % 20 + 1)})
        i += 1
    p.join(10)
    assert q.get(timeout=5) == 0


def test_provider_trip_recorded_under_global(data_dir):
    from core.credit_sentinel import credit_sentinel_active, release
    _trip(None)
    _trip("openrouter")          # arrives while the global trip is fresh
    from core.credit_sentinel import _read_latch, _sentinel_path
    assert "openrouter" in _read_latch(_sentinel_path())
    assert release("*") is True
    assert credit_sentinel_active("openrouter") is True
    assert credit_sentinel_active("anthropic") is False


def test_auto_release_does_not_delete_fresh_trip(data_dir, monkeypatch):
    import core.credit_sentinel as cs
    path = cs._sentinel_path()
    cs._write_latch(path, {"old": {"ts": time.time() - 100 * 3600}})
    real_mutate = cs._mutate

    def racing_mutate(fn):
        # another unit trips between this unit's read and its prune
        cs._write_latch(path, {"old": {"ts": time.time() - 100 * 3600},
                               "openrouter": {"ts": time.time()}})
        return real_mutate(fn)

    monkeypatch.setattr(cs, "_mutate", racing_mutate)
    assert cs.credit_sentinel_active("openrouter") is True
    monkeypatch.undo()
    entries = cs._read_latch(path)
    assert "openrouter" in entries and "old" not in entries


def test_release_all_resets_corrupt_file(data_dir):
    import core.credit_sentinel as cs
    path = cs._sentinel_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("{not json")
    assert cs.credit_sentinel_active() is True     # corrupt reads as a global trip
    assert cs.release(None) is True
    assert cs.credit_sentinel_active() is False
    assert json.loads(open(path).read()) == {"providers": {}}
