"""031 T11: REPL /pause /halt /resume and the prose gate."""


def _ctx(out, args):
    return type("Ctx", (), {"args": args, "container": None,
                            "emit": lambda self, text, title="": out.append(text)})()


def test_repl_pause_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from cli.ui.commands.h_owner import h_halt, h_pause, h_resume
    out = []
    h_pause(_ctx(out, ["streams"]))
    assert out[-1].startswith("⏸ Paused streams")
    h_resume(_ctx(out, []))
    assert "RESUMED" in out[-1]
    h_halt(_ctx(out, ["ignored"]))
    assert out[-1].startswith("⏸ Paused everything")
    h_pause(_ctx(out, ["bogus"]))
    assert "unknown scope" in out[-1]
    h_resume(_ctx(out, []))
    assert "RESUMED" in out[-1]


def test_repl_prose_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from cli.commands.chat import _owner_pause_gate
    from core.autonomy_control import read_state
    container = type("C", (), {"config": type("Cfg", (), {"data_dir": str(tmp_path)})()})()
    assert _owner_pause_gate("continue", container) is None       # nothing paused: chat
    assert _owner_pause_gate("stop trading", container) is None   # scoped: the agent narrows
    assert _owner_pause_gate("why did you stop?", container) is None
    ans = _owner_pause_gate("stop everything", container)
    assert ans.startswith("⏸ Paused everything") and read_state(str(tmp_path)).via == "repl"
    assert _owner_pause_gate("continue", container) is None       # plain "continue" stays chat
    ans = _owner_pause_gate("continue autonomy", container)
    assert ans.startswith("▶ Autonomy RESUMED") and not read_state(str(tmp_path)).paused


def test_pause_and_resume_are_registered_slash_commands():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert {"pause", "halt", "resume"} <= set(reg._commands)
