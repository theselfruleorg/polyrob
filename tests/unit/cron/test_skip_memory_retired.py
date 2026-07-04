"""ME-D2 — retire the dead ``skip_memory`` knob from the public CronService surface.

``skip_memory`` was persisted per cron job and accepted by ``CronService.schedule``
but never consumed by the runner or the goal dispatcher — an inverted, inert no-op
that misled operators. The DB column/dataclass field stay (dormant) to avoid a
schema migration; only the public setter surface is removed.
"""
import inspect
from datetime import datetime

from cron.jobs import CronJobStore
from cron.service import CronService


def test_schedule_no_longer_accepts_skip_memory():
    assert "skip_memory" not in inspect.signature(CronService.schedule).parameters


def test_schedule_and_readback_survives_dormant_column(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    svc = CronService(store, now=lambda: datetime.fromisoformat("2026-06-06T12:00:00"))
    job = svc.schedule(task="t", schedule_spec="1h", user_id="u1")
    # Dormant column keeps its schema default; no crash reading it back.
    got = store.get(job.id)
    assert got is not None
    assert got.skip_memory is True
