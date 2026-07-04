"""T1.4 (read-only slice) — `polyrob update --check/--dry-run/--json` behavior."""
import json

from click.testing import CliRunner

import cli.commands.update as up
from cli.commands.update import (
    EXIT_ERROR, EXIT_UP_TO_DATE, EXIT_UPDATE_AVAILABLE, update_cmd,
)


def _patch(monkeypatch, *, method="git", current="0.4.2", latest="0.5.0"):
    from cli.update.detect import InstallContext
    from pathlib import Path

    ctx = InstallContext(method, Path("/repo"),
                         Path("/repo") if method in {"git", "editable_git"} else None,
                         "test")
    monkeypatch.setattr(up, "detect_install", lambda *a, **k: ctx)

    body_pypi = json.dumps({"releases": {current: [], latest: []}})
    body_gh = json.dumps([{"tag_name": f"v{latest}"}, {"tag_name": f"v{current}"}])
    monkeypatch.setattr(up, "_http_get",
                        lambda url, timeout=6.0: body_gh if "github" in url else body_pypi)
    # Pin current so the test doesn't depend on the real installed version.
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: current)


def test_check_exit_10_when_update_available(monkeypatch):
    _patch(monkeypatch, current="0.4.2", latest="0.5.0")
    res = CliRunner().invoke(update_cmd, ["--check"])
    assert res.exit_code == EXIT_UPDATE_AVAILABLE
    assert "update is available" in res.output.lower()


def test_check_exit_0_when_up_to_date(monkeypatch):
    _patch(monkeypatch, current="0.5.0", latest="0.5.0")
    res = CliRunner().invoke(update_cmd, ["--check"])
    assert res.exit_code == EXIT_UP_TO_DATE
    assert "up to date" in res.output.lower()


def test_json_output_is_machine_readable(monkeypatch):
    _patch(monkeypatch, method="pip", current="0.4.2", latest="0.5.0")
    res = CliRunner().invoke(update_cmd, ["--check", "--json"])
    data = json.loads(res.output)
    assert data["current"] == "0.4.2"
    assert data["latest"] == "0.5.0"
    assert data["method"] == "pip"
    assert data["update_available"] is True


def test_docker_defers_to_manager(monkeypatch):
    _patch(monkeypatch, method="docker", current="0.4.2", latest="0.5.0")
    res = CliRunner().invoke(update_cmd, [])
    assert res.exit_code == EXIT_UP_TO_DATE
    assert "not available for a docker" in res.output.lower()
    assert "docker compose pull" in res.output


def test_offline_reports_could_not_check(monkeypatch):
    from cli.update.detect import InstallContext
    from pathlib import Path
    monkeypatch.setattr(up, "detect_install",
                        lambda *a, **k: InstallContext("pip", Path("/x"), None, "t"))

    def boom(url, timeout=6.0):
        raise OSError("offline")

    monkeypatch.setattr(up, "_http_get", boom)
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: "0.4.2")
    res = CliRunner().invoke(update_cmd, ["--check"])
    assert res.exit_code == EXIT_UP_TO_DATE  # unknown latest => not "available"
    assert "could not check" in res.output.lower()


def test_check_not_found_is_informative(monkeypatch):
    from cli.update.detect import InstallContext
    from pathlib import Path
    monkeypatch.setenv("POLYROB_UPDATE_REPO", "acme/widget")
    monkeypatch.setattr(up, "detect_install",
                        lambda *a, **k: InstallContext("git", Path("/x"), Path("/x"), "t"))

    class _HTTP404(Exception):
        code = 404

    def boom(url, timeout=6.0):
        raise _HTTP404()

    monkeypatch.setattr(up, "_http_get", boom)
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: "0.4.2")
    res = CliRunner().invoke(update_cmd, ["--check"])
    assert res.exit_code == EXIT_UP_TO_DATE
    # Not a bare "could not check" — it names the repo it looked at.
    assert "acme/widget" in res.output


def test_check_json_is_pure_and_carries_error(monkeypatch):
    from cli.update.detect import InstallContext
    from pathlib import Path
    monkeypatch.setenv("POLYROB_UPDATE_REPO", "acme/widget")
    monkeypatch.setattr(up, "detect_install",
                        lambda *a, **k: InstallContext("git", Path("/x"), Path("/x"), "t"))
    monkeypatch.setattr(up, "_http_get", lambda url, timeout=6.0: "[]")
    monkeypatch.setattr("cli.update.versions.installed_version", lambda: "0.4.2")
    res = CliRunner().invoke(update_cmd, ["--check", "--json"])
    data = json.loads(res.output)  # pure JSON, no leaked log lines
    assert data["error"] == "no_releases"
    assert data["source_ref"] == "acme/widget"


def test_apply_supported_method_runs_engine(monkeypatch, tmp_path):
    """--apply on a git install builds runners, guards, and runs the engine."""
    from pathlib import Path

    import cli.commands.update as up
    from cli.update.context import UpdateContext
    from cli.update.detect import InstallContext
    from cli.update.engine import ApplyResult

    _patch(monkeypatch, method="git", current="0.4.2", latest="0.4.3")
    monkeypatch.setattr(up, "build_runners", lambda ctx: object(), raising=False)
    # No server/DB in use, and a captured apply.
    uctx = UpdateContext(data_home=tmp_path, snapshots_root=tmp_path / "s", db_paths=[])
    monkeypatch.setattr(up, "resolve_update_context", lambda *a, **k: uctx)
    monkeypatch.setattr(up, "active_use_reasons", lambda *a, **k: [], raising=False)

    class _Snap:  # minimal snapshot stand-in
        name = "SNAP1"
    seen = {}

    def fake_apply(**kw):
        seen.update(kw)
        return ApplyResult(True, None, None, _Snap(), False)
    monkeypatch.setattr(up, "apply_update", fake_apply, raising=False)

    res = CliRunner().invoke(update_cmd, ["--apply", "--yes"])
    assert res.exit_code == EXIT_UP_TO_DATE, res.output
    assert "Updated to v0.4.3" in res.output
    assert seen.get("from_version") == "0.4.2" and seen.get("to_version") == "v0.4.3"


def test_apply_unsupported_method_prints_manual(monkeypatch):
    import cli.commands.update as up
    _patch(monkeypatch, method="docker", current="0.4.2", latest="0.4.3")
    monkeypatch.setattr(up, "build_runners", lambda ctx: None, raising=False)
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes"])
    assert res.exit_code == EXIT_UP_TO_DATE
    assert "isn't supported for a docker" in res.output


# ---------------------------------------------------------------------------
# --json coverage for the rollback + apply success/failure/lock branches.
# ---------------------------------------------------------------------------


def _apply_env(monkeypatch, tmp_path, *, ok=True):
    """Common --apply harness: git install, update available, nothing in use."""
    from cli.update.context import UpdateContext

    _patch(monkeypatch, method="git", current="0.4.2", latest="0.4.3")
    monkeypatch.setattr(up, "build_runners", lambda ctx: object(), raising=False)
    uctx = UpdateContext(data_home=tmp_path, snapshots_root=tmp_path / "s", db_paths=[])
    monkeypatch.setattr(up, "resolve_update_context", lambda *a, **k: uctx)
    monkeypatch.setattr(up, "active_use_reasons", lambda *a, **k: [], raising=False)


def test_rollback_json_emits_json(monkeypatch, tmp_path):
    """--rollback --json emits a machine-readable success payload (not prose)."""
    import contextlib

    from cli.update.context import UpdateContext

    uctx = UpdateContext(data_home=tmp_path, snapshots_root=tmp_path / "s", db_paths=[])
    monkeypatch.setattr(up, "resolve_update_context", lambda *a, **k: uctx)
    monkeypatch.setattr(up, "active_use_reasons", lambda *a, **k: [], raising=False)

    class _Manifest:
        from_version = "0.4.2"
        items = []  # noqa: RUF012 (test stub)

    class _Target:
        name = "SNAP1"
        manifest = _Manifest()
        path = tmp_path / "s" / "SNAP1"

    class _Restored:
        items = [1, 2]  # noqa: RUF012 (test stub)

    monkeypatch.setattr(up, "latest_complete", lambda *a, **k: _Target(), raising=False)
    monkeypatch.setattr(up, "update_lock",
                        lambda *a, **k: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(up, "restore_snapshot", lambda *a, **k: _Restored(), raising=False)

    res = CliRunner().invoke(update_cmd, ["--rollback", "--yes", "--json"])
    assert res.exit_code == EXIT_UP_TO_DATE, res.output
    data = json.loads(res.output)  # pure JSON, no prose lines
    assert data["rolled_back"] is True
    assert data["snapshot"] == "SNAP1"
    assert data["restored_items"] == 2
    assert data["from_version"] == "0.4.2"


def test_apply_json_success_emits_json(monkeypatch, tmp_path):
    """--apply --json success emits a machine-readable payload (not prose)."""
    from cli.update.engine import ApplyResult

    _apply_env(monkeypatch, tmp_path)

    class _Snap:
        name = "SNAP1"

    monkeypatch.setattr(up, "apply_update",
                        lambda **kw: ApplyResult(True, None, None, _Snap(), False),
                        raising=False)
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes", "--json"])
    assert res.exit_code == EXIT_UP_TO_DATE, res.output
    data = json.loads(res.output)  # pure JSON, no "Updating …/Steps …" prose
    assert data["applied"] is True
    assert data["to_version"] == "v0.4.3"
    assert data["from_version"] == "0.4.2"
    assert data["snapshot"] == "SNAP1"


def test_apply_json_failure_emits_json(monkeypatch, tmp_path):
    """--apply --json failure emits a machine-readable payload and exits non-zero."""
    from cli.update.engine import ApplyResult

    _apply_env(monkeypatch, tmp_path)

    class _Snap:
        name = "SNAP1"

    monkeypatch.setattr(up, "apply_update",
                        lambda **kw: ApplyResult(False, "install", "boom", _Snap(), True),
                        raising=False)
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes", "--json"])
    assert res.exit_code == EXIT_ERROR, res.output
    data = json.loads(res.output)
    assert data["applied"] is False
    assert data["failed_step"] == "install"
    assert data["error"] == "boom"
    assert data["snapshot"] == "SNAP1"


def test_apply_lock_held_is_clean_error(monkeypatch, tmp_path):
    """A held update lock during --apply prints a clean error (no traceback), exits non-zero."""
    from cli.update.process_guard import UpdateLockHeld

    _apply_env(monkeypatch, tmp_path)

    def _held(*a, **k):
        raise UpdateLockHeld("another update is in progress")

    monkeypatch.setattr(up, "update_lock", _held, raising=False)

    def _never(**kw):
        raise AssertionError("apply_update should not run when the lock is held")

    monkeypatch.setattr(up, "apply_update", _never, raising=False)

    # Text mode: clean message, no traceback, no leaked exception.
    res = CliRunner().invoke(update_cmd, ["--apply", "--yes"])
    assert res.exit_code == EXIT_ERROR, res.output
    assert "Apply failed" in res.output
    assert "Traceback" not in res.output
    assert not isinstance(res.exception, UpdateLockHeld)

    # JSON mode: structured error.
    res2 = CliRunner().invoke(update_cmd, ["--apply", "--yes", "--json"])
    assert res2.exit_code == EXIT_ERROR, res2.output
    data = json.loads(res2.output)
    assert data["applied"] is False
    assert "error" in data
