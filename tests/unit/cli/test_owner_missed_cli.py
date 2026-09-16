"""`polyrob owner missed` — A7 / A40: the CLI seat for owner notices the
delivery rail could not send live (capped / paused / undelivered)."""
import time

from click.testing import CliRunner


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")


def test_missed_empty(tmp_path, monkeypatch):
    from core.event_log import TelemetryEventLog
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    res = CliRunner().invoke(owner, ["missed"])
    assert res.exit_code == 0
    assert "no missed owner messages" in res.output


def test_missed_lists_every_marker(tmp_path, monkeypatch):
    from core.event_log import TelemetryEventLog
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    for t in ("[suppressed by daily proactive-message cap; source=a] capped one",
              "[held by owner pause; source=b] paused one",
              "[undelivered; source=c] undelivered one"):
        log.record("owner_notice", user_id="alice", source="user_delivery", attrs={"text": t})
    res = CliRunner().invoke(owner, ["missed"])
    assert res.exit_code == 0
    assert "[capped] capped one" in res.output
    assert "[paused] paused one" in res.output
    assert "[undelivered] undelivered one" in res.output
    assert "delivery.daily_cap" in res.output


def test_missed_wraps_long_text_without_clipping(tmp_path, monkeypatch):
    """Fix round 1 (2026-09-14): a long notice must be WRAPPED, never
    truncated — /missed exists to recover text the owner never received, so
    hiding part of it defeats the point. Every printed line stays <= 80
    columns, but the full notice is still readable across the wrapped lines."""
    from core.event_log import TelemetryEventLog
    from cli.commands.owner import owner
    _env(tmp_path, monkeypatch)
    ts = 1_700_000_000.0
    words = [f"word{i:03d}" for i in range(40)]
    long_text = " ".join(words)
    assert len(long_text) >= 300
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("owner_notice", user_id="alice", source="user_delivery", ts=ts,
               attrs={"text": f"[undelivered; source=a] {long_text}"})
    res = CliRunner().invoke(owner, ["missed"])
    assert res.exit_code == 0

    lines = res.output.splitlines()
    assert lines, "expected output"
    assert all(len(line) <= 80 for line in lines)  # (a) every line fits 80 cols

    stamp = time.strftime("%m-%d %H:%M", time.gmtime(ts))
    prefix = f"  {stamp}Z — [undelivered] "
    indent = " " * len(prefix)
    chunks = []
    for line in lines:
        if line.startswith(prefix):
            chunks.append(line[len(prefix):])
        elif chunks and line.startswith(indent) and line.strip():
            chunks.append(line[len(indent):])
    # (b) the full 300+ char notice survives across the wrapped lines
    assert " ".join(chunks) == long_text
