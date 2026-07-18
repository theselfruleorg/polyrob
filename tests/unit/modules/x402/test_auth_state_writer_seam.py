"""R-4: modules/x402 must not import api.* — api installs the auth-state writer."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_x402_middleware_has_no_api_import():
    tree = ast.parse((ROOT / "modules" / "x402" / "middleware.py").read_text())
    offenders = [
        n.module for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
        and (n.module == "api" or n.module.startswith("api."))
    ]
    assert not offenders, f"modules imports the api tier: {offenders}"


def test_writer_seam_installs_and_resets():
    from modules.x402 import middleware as mw
    orig = mw._AUTH_STATE_WRITER
    try:
        marker = object()
        mw.install_auth_state_writer(marker)
        assert mw._AUTH_STATE_WRITER is marker
    finally:
        mw._AUTH_STATE_WRITER = orig
