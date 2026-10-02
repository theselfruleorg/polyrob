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


# --- OB1 / OB12 (2026-10-03 audit) -------------------------------------------

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_auto_open_goes_half_open_after_cooldown():
    """OB1: an auto-opened breaker must let a probe through after the cooldown —
    before, only a successful send closed it and an open breaker never sent."""
    clock = _Clock()
    cb = SurfaceCircuitBreaker(threshold=2, cooldown=60, clock=clock)
    cb.record_fail("tg"); cb.record_fail("tg")
    assert cb.is_open("tg") is True
    clock.t += 59
    assert cb.is_open("tg") is True
    clock.t += 2
    assert cb.is_open("tg") is False        # half-open: probe allowed


def test_failed_probe_reopens_for_another_cooldown():
    clock = _Clock()
    cb = SurfaceCircuitBreaker(threshold=2, cooldown=60, clock=clock)
    cb.record_fail("tg"); cb.record_fail("tg")
    clock.t += 61
    assert cb.is_open("tg") is False
    cb.record_fail("tg")                     # the probe failed
    assert cb.is_open("tg") is True
    clock.t += 61
    assert cb.is_open("tg") is False
    cb.record_ok("tg")                       # the probe worked
    assert cb.is_open("tg") is False
    assert cb.state("tg")["consecutive_failures"] == 0


def test_auto_open_is_visible_to_another_process(tmp_path):
    """OB12: the worker's auto-open is persisted, so a reader in another process
    (health / the CLI) does not show 'closed' while the queue is held."""
    from core.surfaces.circuit import CircuitStore
    store = CircuitStore(str(tmp_path / "surface_state.db"))
    clock = _Clock()
    worker = SurfaceCircuitBreaker(threshold=2, store=store, cooldown=60, clock=clock)
    reader = SurfaceCircuitBreaker(threshold=2, store=store, cooldown=60, clock=clock)
    worker.record_fail("tg"); worker.record_fail("tg")
    assert reader.is_open("tg") is True
    assert reader.state("tg")["auto_open"] is True
    worker.record_ok("tg")
    assert reader.is_open("tg") is False


def test_cli_resume_closes_the_workers_auto_open(tmp_path):
    """OB1: `polyrob surface resume` (another process, store-only) closes the
    worker's in-memory auto-open too."""
    from core.surfaces.circuit import CircuitStore
    store = CircuitStore(str(tmp_path / "surface_state.db"))
    clock = _Clock()
    worker = SurfaceCircuitBreaker(threshold=2, store=store, cooldown=600, clock=clock)
    worker.record_fail("tg"); worker.record_fail("tg")
    assert worker.is_open("tg") is True
    CircuitStore(str(tmp_path / "surface_state.db")).resume("tg")   # the CLI
    assert worker.is_open("tg") is False
    assert worker.state("tg")["consecutive_failures"] == 0


def test_store_migrates_a_pre_auto_open_table(tmp_path):
    import sqlite3
    from core.surfaces.circuit import CircuitStore
    db = str(tmp_path / "surface_state.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE surface_state (surface_id TEXT PRIMARY KEY, "
              "paused INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO surface_state VALUES ('wa', 1)")
    c.commit(); c.close()
    store = CircuitStore(db)
    assert store.is_paused("wa") is True
    store.set_auto_open("wa", 5.0)
    assert store.auto_open_at("wa") == 5.0
    store.resume("wa")
    assert store.auto_open_at("wa") is None
