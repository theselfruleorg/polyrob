"""/telemetry CLI command (telemetry audit 2026-07-04): operator surface that reads
the durable event log so it isn't a write-only store."""


def test_parse_window_seconds():
    from cli.ui.commands.handlers import _parse_window_seconds
    assert _parse_window_seconds("30m") == 1800
    assert _parse_window_seconds("24h") == 86400
    assert _parse_window_seconds("7d") == 604800
    assert _parse_window_seconds("") is None
    assert _parse_window_seconds("garbage") is None


def test_telemetry_command_emits_counts(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    # 120 W3: the read is core.telemetry_view over the resolved store, read-only.
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "te.db"))
    log.record("cron_run", outcome="done")
    log.record("wallet_spend", amount_usd=4.0)
    log.record("tool_denied", action="x402_pay")

    from cli.ui.commands.registry import CommandContext
    from cli.ui.commands.handlers import _h_telemetry

    emitted = []
    ctx = CommandContext()
    ctx.emit = lambda text, **k: emitted.append(text)  # type: ignore
    ctx.args = []
    _h_telemetry(ctx)

    body = "\n".join(emitted)
    assert "cron_run: 1" in body
    assert "wallet_spend: 1" in body
    assert "tool_denied: 1" in body
    assert "$4.0000" in body


def test_telemetry_command_names_an_absent_store(tmp_path, monkeypatch):
    """120 W3: no store is "could not be read", never zero totals, and the read
    does not create the file."""
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "none.db"))
    from cli.ui.commands.registry import CommandContext
    from cli.ui.commands.handlers import _h_telemetry

    emitted = []
    ctx = CommandContext()
    ctx.emit = lambda text, **k: emitted.append(text)  # type: ignore
    ctx.args = []
    _h_telemetry(ctx)
    assert "could not be read" in "\n".join(emitted)
    assert not (tmp_path / "none.db").exists()
