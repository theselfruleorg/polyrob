"""``pm()`` lives in the core tier (proposal 125 S29).

It imports nothing from ``agents``, so every tier reaches it downward; the old
``agents.task.path`` module is gone.
"""
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]


def test_the_old_agents_path_is_gone():
    import importlib.util
    assert importlib.util.find_spec("agents.task.path") is None


def test_one_singleton_through_the_package_export(monkeypatch):
    import core.session_paths as sp
    from agents.task import pm as pkg_pm
    sentinel = object()
    monkeypatch.setattr(sp, "_INSTANCE", None)
    sp.set_path_manager(sentinel)
    assert sp.pm() is sentinel and pkg_pm() is sentinel


def test_session_paths_imports_no_agents_module():
    code = ("import sys, core.session_paths as s; s.PathManager; "
            "leaked = sorted(m for m in sys.modules if m == 'agents' or m.startswith('agents.')); "
            "print(leaked); raise SystemExit(1 if leaked else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=_REPO, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_pack_facade_exports_pm():
    import polyrob.sdk as sdk
    import core.session_paths as sp
    assert sdk.pm is sp.pm
