"""An unreadable update.journal.json is named, never a traceback, and never blocks
every later apply for ever: --rollback and --apply --force set it aside."""
import json
from contextlib import nullcontext
from types import SimpleNamespace

from click.testing import CliRunner

import cli.commands.update as up
from cli.commands.update import EXIT_ERROR, EXIT_UP_TO_DATE, update_cmd
from cli.update.journal import JOURNAL_NAME, clear_failed, recover_open_update


def _torn(root):
    root.mkdir(parents=True, exist_ok=True)
    p = root / JOURNAL_NAME
    p.write_text("{not json")
    return p


def test_recover_names_an_unreadable_journal(tmp_path):
    _torn(tmp_path)
    said = []
    out = recover_open_update(tmp_path, rollback=lambda r: {"ok": True}, echo=said.append)
    assert out == "unreadable"
    text = " ".join(said)
    assert "unreadable" in text and "NOT rolled back" in text
    assert "polyrob update --rollback" in text


def test_clear_failed_sets_an_unreadable_journal_aside(tmp_path):
    p = _torn(tmp_path)
    clear_failed(tmp_path)
    assert not p.exists()
    kept = list(tmp_path.glob(JOURNAL_NAME + ".unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text() == "{not json"


def _rig(monkeypatch, tmp_path):
    from cli.update.context import UpdateContext
    from cli.update.detect import InstallContext
    from cli.update.engine import ApplyResult
    from pathlib import Path
    monkeypatch.setattr(up, "detect_install",
                        lambda *a, **k: InstallContext("git", Path("/repo"), Path("/repo"), "test"))
    body_pypi = json.dumps({"releases": {"0.4.2": [], "0.4.3": []}})
    body_gh = json.dumps([{"tag_name": "v0.4.3"}, {"tag_name": "v0.4.2"}])
    monkeypatch.setattr(up, "_http_get",
                        lambda url, timeout=6.0: body_gh if "github" in url else body_pypi)
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: "0.4.2")
    monkeypatch.setattr(up, "build_runners", lambda ctx, **kw: object(), raising=False)
    uctx = UpdateContext(data_home=tmp_path, snapshots_root=tmp_path / "s", db_paths=[])
    monkeypatch.setattr(up, "resolve_update_context", lambda *a, **k: uctx)
    monkeypatch.setattr(up, "active_use_reasons", lambda *a, **k: [], raising=False)
    from cli.update import agent_state as st
    monkeypatch.setattr(st, "observe", lambda home, **kw: st.AgentActivity())

    class _Snap:
        name = "SNAP1"
    applied = []
    monkeypatch.setattr(up, "apply_update",
                        lambda **kw: applied.append(kw) or ApplyResult(True, None, None, _Snap(), False),
                        raising=False)
    return uctx, applied


def test_apply_refuses_cleanly_on_an_unreadable_journal(monkeypatch, tmp_path):
    uctx, applied = _rig(monkeypatch, tmp_path)
    p = _torn(uctx.snapshots_root)
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes", "--json"])
    assert res.exit_code == EXIT_ERROR, res.output
    data = json.loads(res.output.strip().splitlines()[-1])
    assert data == {"applied": False, "error": "journal_unreadable", "path": str(p)}
    assert not applied and p.exists()

    res = CliRunner().invoke(update_cmd, ["--apply", "--yes"])
    assert res.exit_code == EXIT_ERROR
    assert "Traceback" not in res.output and "unreadable" in res.output
    assert "--rollback" in res.output


def test_apply_force_sets_the_unreadable_journal_aside_and_applies(monkeypatch, tmp_path):
    uctx, applied = _rig(monkeypatch, tmp_path)
    p = _torn(uctx.snapshots_root)
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes", "--force"])
    assert res.exit_code == EXIT_UP_TO_DATE, res.output
    assert applied and not p.exists()
    assert list(uctx.snapshots_root.glob(JOURNAL_NAME + ".unreadable-*"))


def test_boot_apply_that_raises_before_any_change_says_not_applied(monkeypatch):
    from cli.update.detect import InstallContext
    from pathlib import Path
    monkeypatch.setattr(up, "detect_install",
                        lambda *a, **k: InstallContext("git", Path("/repo"), Path("/repo"), "test"))
    body = json.dumps([{"tag_name": "v0.5.0"}, {"tag_name": "v0.4.2"}])
    monkeypatch.setattr(up, "_http_get", lambda url, timeout=6.0: body)
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: "0.4.2")
    monkeypatch.setattr(up, "build_runners", lambda ctx, **kw: SimpleNamespace(state={}))
    monkeypatch.setattr(up, "resolve_update_context",
                        lambda: SimpleNamespace(db_paths=[], snapshots_root=None))
    monkeypatch.setattr(up, "active_use_reasons", lambda _p: [])
    monkeypatch.setattr(up, "update_lock", lambda _r: nullcontext())

    def _snap_fails(**kw):
        raise OSError("disk full")
    monkeypatch.setattr(up, "apply_update", _snap_fails)
    res = up.apply_for_boot("stable")
    assert not res["ok"] and res["not_applied"] and not res["rolled_back"]
