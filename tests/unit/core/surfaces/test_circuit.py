"""Unit tests for SurfaceCircuitBreaker (task 5.3)."""
from core.surfaces.circuit import SurfaceCircuitBreaker


def test_opens_after_k_consecutive_failures():
    cb = SurfaceCircuitBreaker(threshold=3)
    for _ in range(3): cb.record_fail("wa")
    assert cb.is_open("wa") is True
    cb.record_ok("wa")
    assert cb.is_open("wa") is False


def test_manual_pause_resume():
    cb = SurfaceCircuitBreaker(threshold=99)
    cb.pause("wa"); assert cb.is_open("wa") is True
    cb.resume("wa"); assert cb.is_open("wa") is False


def test_two_below_threshold_does_not_open():
    cb = SurfaceCircuitBreaker(threshold=3)
    cb.record_fail("tg")
    cb.record_fail("tg")
    assert cb.is_open("tg") is False


def test_record_ok_resets_counter():
    cb = SurfaceCircuitBreaker(threshold=3)
    cb.record_fail("tg")
    cb.record_fail("tg")
    cb.record_ok("tg")
    cb.record_fail("tg")   # only 1 after the reset → still closed
    assert cb.is_open("tg") is False


def test_independent_surfaces():
    cb = SurfaceCircuitBreaker(threshold=2)
    cb.record_fail("wa")
    cb.record_fail("wa")
    assert cb.is_open("wa") is True
    assert cb.is_open("tg") is False   # tg has no failures


def test_state_returns_snapshot():
    cb = SurfaceCircuitBreaker(threshold=2)
    cb.record_fail("wa")
    s = cb.state("wa")
    assert s["surface_id"] == "wa"
    assert s["consecutive_failures"] == 1
    assert s["threshold"] == 2
    assert s["is_open"] is False
    cb.record_fail("wa")
    assert cb.state("wa")["is_open"] is True


def test_resume_also_resets_counter():
    """resume() should clear both the manual-pause flag AND the counter."""
    cb = SurfaceCircuitBreaker(threshold=3)
    cb.record_fail("wa")
    cb.record_fail("wa")
    cb.record_fail("wa")   # auto-open
    cb.pause("wa")          # also manually paused
    assert cb.is_open("wa") is True
    cb.resume("wa")
    assert cb.is_open("wa") is False
    assert cb.state("wa")["consecutive_failures"] == 0


def test_circuit_store_persists_pause(tmp_path):
    """CircuitStore persists the pause flag across separate breaker instances."""
    from core.surfaces.circuit import CircuitStore
    db = str(tmp_path / "surface_state.db")
    store = CircuitStore(db)

    # Writer breaker (simulates the CLI process)
    writer_cb = SurfaceCircuitBreaker(threshold=99, store=store)
    writer_cb.pause("wa")

    # Reader breaker (simulates the worker process — separate in-memory state)
    reader_cb = SurfaceCircuitBreaker(threshold=99, store=store)
    assert reader_cb.is_open("wa") is True   # reads from store

    # resume via writer → reader sees it
    writer_cb.resume("wa")
    assert reader_cb.is_open("wa") is False


def test_is_open_fail_open_on_store_error():
    """A raising persisted store must NOT crash is_open/state — treat as not paused."""
    class _BadStore:
        def is_paused(self, surface_id):
            raise RuntimeError("db corrupt")
    cb = SurfaceCircuitBreaker(threshold=3, store=_BadStore())
    assert cb.is_open("wa") is False          # no raise; degrades to not-paused
    assert cb.state("wa")["store_paused"] is False
