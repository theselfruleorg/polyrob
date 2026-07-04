import re
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "no version = in pyproject.toml"
    return m.group(1)

def test_pyproject_is_0_4_2():
    assert _pyproject_version() == "0.4.2"

def test_core_version_accessor_matches_pyproject():
    from core.version import get_version
    # In an editable/installed env importlib returns the installed version;
    # the dev fallback must equal pyproject so the two never diverge.
    assert get_version() == _pyproject_version()

def test_core_dunder_version_is_accessor():
    import core.version as cv
    assert cv.__version__ == cv.get_version()

def test_source_pyproject_wins_over_stale_installed_metadata(monkeypatch):
    """A stale editable/wheel install (e.g. polyrob pinned at an old 1.0.0) must NOT
    shadow the source version — running `polyrob` from this checkout must report the
    checkout's version. This is the reported 'CLI provides wrong version' bug."""
    import core.version as cv
    monkeypatch.setattr(cv, "_pkg_version", lambda name: "1.0.0")  # stale install
    assert cv.get_version() == _pyproject_version()  # source pyproject wins

def test_falls_back_to_installed_metadata_without_source(monkeypatch):
    """When there's no adjacent source pyproject (a real pip install into
    site-packages), installed metadata is used."""
    import core.version as cv
    monkeypatch.setattr(cv, "_source_pyproject_version", lambda: None)
    monkeypatch.setattr(cv, "_pkg_version", lambda name: "9.9.9")
    assert cv.get_version() == "9.9.9"

def test_fallback_pinned_to_pyproject():
    from core.version import _FALLBACK_VERSION
    assert _FALLBACK_VERSION == _pyproject_version()

def test_cli_version_is_ssot():
    from core.version import get_version
    import cli.polyrob as p
    assert p.VERSION == get_version()

def test_fastapi_app_version_is_ssot():
    from core.version import get_version
    from api.app import create_app
    app = create_app()
    assert app.version == get_version()

def test_agent_card_default_version_is_ssot():
    from core.version import get_version
    from api.a2a.agent_card import build_agent_card
    card = build_agent_card()
    assert card.version == get_version()

def test_mcp_client_version_is_ssot():
    """The MCP client advertises get_version() as clientInfo.version, not a literal."""
    from core.version import get_version
    from tools.mcp.protocol import _client_version
    assert _client_version() == get_version()

def test_webview_version_is_ssot(monkeypatch):
    """webview _version() routes through get_version() (source wins), not stale
    installed metadata; an explicit WEBVIEW_VERSION override still wins."""
    from core.version import get_version
    from webview.pages import _version
    monkeypatch.delenv("WEBVIEW_VERSION", raising=False)
    assert _version() == get_version()
    monkeypatch.setenv("WEBVIEW_VERSION", "9.9.9")
    assert _version() == "9.9.9"

def test_no_stray_project_version_literal():
    """No runtime source file should hardcode a project-style version literal."""
    import subprocess

    # Files allowed to contain a hardcoded project-style version literal.
    _ALLOWED = {
        "core/version.py",                       # the one dev fallback
        "pyproject.toml",                        # build SSOT
        "migrations/version_manager.py",         # DB schema version (independent)
    }

    out = subprocess.run(
        ["git", "grep", "-nE", r"['\"]1\.0\.0['\"]",
         "--", "agents", "api", "cli", "core", "modules", "surfaces",
         "tools", "utils", "webview"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True,
    ).stdout
    offenders = [
        ln for ln in out.splitlines()
        if not any(ln.startswith(a) for a in _ALLOWED)
        # The DB schema baseline (schema.sql) is legitimately versioned 1.0.0 and is
        # independent of the project version. Everything else — including the MCP
        # clientInfo version and any endpoint version — must route through
        # get_version(), so no other file may hardcode the old project literal.
        and "schema.sql" not in ln
    ]
    assert not offenders, "stray project-version literal:\n" + "\n".join(offenders)
