"""I-9: webview/CLI data-home convergence.

Locally (``POLYROB_DATA_DIR`` unset) the CLI/agent resolves its data home to
``cwd/.polyrob`` (``core.bootstrap._resolve_cli_data_home``), but
``webview.pages._data_dir``/``webview.activity._data_dir`` fell back to the
unrelated ``./data`` — so a local ``rob`` session writes goals/cron/memory to
one place while the webview console reads from another, empty, one.

This mirrors the pattern ``webview.pages._pfp_data_dir`` already uses for the
avatar (env wins; else prefer the CLI's ``cwd/.polyrob`` default), extended to
the general ``_data_dir()`` used by the goals/cron/memory/identity endpoints
in both ``pages.py`` and ``activity.py``.

Landmine (prod): the webview ships standalone via ``scripts/deploy_webview.sh``,
where ``core`` may not be on the import path — behavior in that case MUST stay
the legacy fallback (env, else ``./data``), never raise.
"""
import sys

import pytest


# --------------------------------------------------------------------------- #
# POLYROB_DATA_DIR set -> env always wins, unchanged from before (both modules)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("modname", ["webview.pages", "webview.activity"])
def test_env_set_wins(monkeypatch, modname):
    import importlib
    mod = importlib.import_module(modname)
    monkeypatch.setenv("POLYROB_DATA_DIR", "/explicit/home")
    assert mod._data_dir() == "/explicit/home"


# --------------------------------------------------------------------------- #
# POLYROB_DATA_DIR unset, core.bootstrap importable -> converges on the CLI's
# own resolution (cwd/.polyrob), not the stale ./data default.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("modname", ["webview.pages", "webview.activity"])
def test_env_unset_converges_on_cli_data_home(monkeypatch, tmp_path, modname):
    import importlib
    mod = importlib.import_module(modname)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    from core.bootstrap import _resolve_cli_data_home
    expected_home, _ws_is_project_root, _project_root = _resolve_cli_data_home()

    assert mod._data_dir() == str(expected_home)
    assert mod._data_dir() == str(tmp_path / ".polyrob")


def test_pages_and_activity_agree_with_each_other(monkeypatch, tmp_path):
    """The two independently-defined ``_data_dir()`` functions must resolve to
    the SAME path — this is the actual webview<->CLI convergence bug (a local
    agent session and the webview console reading/writing different homes)."""
    import webview.activity as activity
    import webview.pages as pages

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert pages._data_dir() == activity._data_dir() == str(tmp_path / ".polyrob")


def test_webview_delegates_to_the_shared_core_seam(monkeypatch, tmp_path):
    """pages/activity must not carry their own resolution logic anymore: both
    delegate to ``webview.webgate.data_dir()``, which resolves through the ONE
    core seam (``core.runtime_paths.resolve_data_home``) shared with the CLI
    admin verbs (2026-07-12 UI-surface review dedupe)."""
    import webview.activity as activity
    import webview.pages as pages
    import webview.webgate as webgate
    from core.runtime_paths import resolve_data_home

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert (
        pages._data_dir()
        == activity._data_dir()
        == webgate.data_dir()
        == str(resolve_data_home())
    )


# --------------------------------------------------------------------------- #
# core.bootstrap unimportable (standalone webview deploy) -> legacy fallback,
# never raises.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("modname", ["webview.pages", "webview.activity"])
def test_core_unimportable_falls_back_to_legacy_default(monkeypatch, tmp_path, modname):
    import importlib
    mod = importlib.import_module(modname)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    # A module set to None in sys.modules makes importing it raise ImportError
    # (CPython import-system contract) -- simulates a standalone webview deploy
    # where `core` isn't on the path, without needing a dedicated seam. The
    # resolution seam is core.runtime_paths (resolve_data_home).
    monkeypatch.setitem(sys.modules, "core.runtime_paths", None)

    assert mod._data_dir() == "data"
