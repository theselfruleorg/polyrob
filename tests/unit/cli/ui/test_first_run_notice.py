"""0.9.0 first-run posture notice — pure builder + print-once behavior."""
import cli.ui.first_run_notice as frn


class _FakeRenderer:
    def __init__(self):
        self.blocks = []

    def print_block(self, text):
        self.blocks.append(text)


def test_build_notice_off_mentions_autonomy_data_config_and_enable_hint():
    lines = frn.build_first_run_notice(
        autonomy_on=False, data_dir="/home/u/.polyrob", config_path="/home/u/.polyrob/.env")
    blob = "\n".join(lines)
    assert "Autonomy: OFF" in blob
    assert "/home/u/.polyrob" in blob
    assert "/home/u/.polyrob/.env" in blob
    assert "Interactive tools on" in blob
    assert "AUTONOMY_ENABLED=true" in blob  # the enable hint


def test_build_notice_on_has_no_enable_hint():
    lines = frn.build_first_run_notice(
        autonomy_on=True, data_dir="/d", config_path=None)
    blob = "\n".join(lines)
    assert "Autonomy: ON" in blob
    assert "AUTONOMY_ENABLED=true" not in blob
    # config line omitted when no config path resolved
    assert "Config:" not in blob


def test_notice_prints_once_and_writes_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(frn, "_resolve_context", lambda: (False, str(tmp_path), None))
    r = _FakeRenderer()
    assert frn.maybe_print_first_run_notice(r) is True
    assert len(r.blocks) == 1
    assert frn.marker_path(str(tmp_path)).exists()
    # second call is a no-op (marker present)
    r2 = _FakeRenderer()
    assert frn.maybe_print_first_run_notice(r2) is False
    assert r2.blocks == []


def test_notice_fail_open_on_resolver_error(monkeypatch):
    def _boom():
        raise RuntimeError("resolve blew up")
    monkeypatch.setattr(frn, "_resolve_context", _boom)
    # must not raise; returns False
    assert frn.maybe_print_first_run_notice(_FakeRenderer()) is False
