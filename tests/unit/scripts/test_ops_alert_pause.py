"""031 T14: ops_alert.py honours the owner pause (oversight scope) unless --critical."""
import importlib.util
import json
import pathlib


def _load():
    p = pathlib.Path("scripts/ops_alert.py")
    spec = importlib.util.spec_from_file_location("ops_alert_pause_t", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_pause_active_for_reads_record(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    m = _load()
    assert m.pause_active_for("oversight") is None
    (tmp_path / "AUTONOMY_PAUSE.json").write_text(json.dumps(
        {"paused": True, "scopes": ["oversight"], "since": 1.0, "until": None}))
    assert m.pause_active_for("oversight") == ["oversight"]
    (tmp_path / "AUTONOMY_PAUSE.json").write_text(json.dumps(
        {"paused": True, "scopes": ["trading"], "since": 1.0, "until": None}))
    assert m.pause_active_for("oversight") is None
    (tmp_path / "AUTONOMY_PAUSE.json").write_text(json.dumps(
        {"paused": True, "scopes": ["all"], "since": 1.0, "until": 1.0}))  # expired
    assert m.pause_active_for("oversight") is None
    (tmp_path / "AUTONOMY_PAUSE.json").write_text("{broken")
    assert m.pause_active_for("oversight") == ["all"]


def test_main_suppresses_non_critical_while_paused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "AUTONOMY_PAUSE.json").write_text(json.dumps({"paused": True, "scopes": ["all"]}))
    m = _load()
    monkeypatch.setattr(m, "send", lambda text: (_ for _ in ()).throw(AssertionError("must not send")))
    assert m.main(["hello", "owner"]) == 0
    assert "suppressed: paused" in capsys.readouterr().out
    assert "hello owner" in (tmp_path / "ops_alert_suppressed.log").read_text()
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    assert m.main(["service", "crashed", "--critical"]) == 0
    assert sent and "service crashed" in sent[0]
