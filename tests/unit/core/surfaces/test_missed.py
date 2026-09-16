"""A7 / A40: `core.surfaces.missed.missed_notices` — the ONE query every seat
(Telegram, CLI, REPL) reads for "what did I miss?".

Before this module existed, only Telegram's `/missed` could read suppressed
notices, and it matched ONLY the daily-cap marker — a `paused` or
`undelivered` (fallback) notice was unreadable anywhere.
"""
import os

import pytest

from core.event_log import TelemetryEventLog
from core.surfaces.missed import missed_notices


def test_missed_reads_every_marker(tmp_path):
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    for t in ("[suppressed by daily proactive-message cap; source=a] one",
              "[held by owner pause; source=b] two", "[undelivered; source=c] three"):
        log.record("owner_notice", user_id="u1", source="user_delivery", attrs={"text": t})
    texts = [n["text"] for n in missed_notices("u1", str(tmp_path), 10)]
    assert texts == ["three", "two", "one"]


def test_missed_returns_kind_and_strips_marker(tmp_path):
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("owner_notice", user_id="u1", source="user_delivery",
               attrs={"text": "[held by owner pause; source=x] the body"})
    rows = missed_notices("u1", str(tmp_path), 5)
    assert len(rows) == 1
    assert rows[0]["kind"] == "paused"
    assert rows[0]["text"] == "the body"
    assert isinstance(rows[0]["ts"], float)


def test_missed_is_tenant_scoped(tmp_path):
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("owner_notice", user_id="u1", source="user_delivery",
               attrs={"text": "[undelivered; source=a] mine"})
    log.record("owner_notice", user_id="other", source="user_delivery",
               attrs={"text": "[undelivered; source=a] not mine"})
    rows = missed_notices("u1", str(tmp_path), 10)
    assert [r["text"] for r in rows] == ["mine"]


def test_missed_ignores_unmarked_owner_notices(tmp_path):
    """A owner_notice row that doesn't carry one of the three markers (some
    other, unrelated notice kind) must not show up in /missed."""
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("owner_notice", user_id="u1", source="something_else",
               attrs={"text": "unrelated notice, no marker"})
    rows = missed_notices("u1", str(tmp_path), 10)
    assert rows == []


def test_missed_empty_when_store_readable_but_no_rows(tmp_path):
    TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    assert missed_notices("u1", str(tmp_path), 10) == []


def test_missed_raises_on_unreadable_store(tmp_path):
    """A store that cannot be read must never be reported as 'nothing to
    show' — the caller renders the reason."""
    missing_dir = str(tmp_path / "does-not-exist")
    with pytest.raises(Exception):
        missed_notices("u1", missing_dir, 5)


def test_missed_honours_telemetry_event_log_path_override(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    override_path = str(other / "telemetry_events.db")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", override_path)
    log = TelemetryEventLog(override_path)
    log.record("owner_notice", user_id="u1", source="user_delivery",
               attrs={"text": "[undelivered; source=a] via override"})
    # data_dir points somewhere ELSE — the env override must win.
    rows = missed_notices("u1", str(tmp_path), 10)
    assert [r["text"] for r in rows] == ["via override"]
