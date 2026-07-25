from cli.ui.commands import autonomy_status_lines


def test_autonomy_lines_report_flag_state(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("AUTONOMY_ENABLED", "1")   # 0.9.0: master flips the loop group on
    monkeypatch.setenv("GOALS_ENABLED", "false")  # explicit off wins
    lines = autonomy_status_lines("u1", str(tmp_path))
    blob = "\n".join(lines)
    assert "local mode (POLYROB_LOCAL): on" in blob
    assert "autonomy (AUTONOMY_ENABLED): on" in blob
    assert "self-wake=on" in blob
    assert "goals=off" in blob
    # No stores seeded → fail-open, not a crash.
    assert "cron jobs:" in blob
    assert "goals (open):" in blob


def test_autonomy_lines_local_without_master_keeps_loops_off(monkeypatch, tmp_path):
    # POLYROB_LOCAL alone no longer flips the autonomy loops on (autonomy OFF default).
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("AUTONOMY_ENABLED", raising=False)
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    monkeypatch.delenv("SELF_WAKE_ENABLED", raising=False)
    lines = autonomy_status_lines("u1", str(tmp_path))
    blob = "\n".join(lines)
    assert "local mode (POLYROB_LOCAL): on" in blob
    assert "autonomy (AUTONOMY_ENABLED): off" in blob
    assert "self-wake=off" in blob


def test_autonomy_lines_default_off(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("AUTONOMY_ENABLED", raising=False)
    monkeypatch.delenv("SELF_WAKE_ENABLED", raising=False)
    lines = autonomy_status_lines("u1", str(tmp_path))
    blob = "\n".join(lines)
    assert "local mode (POLYROB_LOCAL): off" in blob
    assert "autonomy (AUTONOMY_ENABLED): off" in blob
    assert "self-wake=off" in blob
