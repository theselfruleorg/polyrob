"""Phase 1 (path-concerns upgrade): construction/import must not pollute CWD.

A1: BotConfig() created ./data/{simulations,...} relative to CWD because
_ensure_directories ran its mkdir loop against the still-relative default
data_dir BEFORE absolute-izing.
N1: importing `core` created a top-level ./logs relative to CWD via
core.logging.DEFAULT_LOG_DIR = Path('logs') (import-time side effect).

Both must resolve against the install/base dir, never the caller's CWD.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_default_log_dir_is_absolute_under_repo_root():
    """N1: the module-level log dir must be CWD-independent (anchored to repo)."""
    import core.logging as cl

    assert cl.DEFAULT_LOG_DIR.is_absolute(), cl.DEFAULT_LOG_DIR
    assert cl.DEFAULT_LOG_DIR == REPO_ROOT / "logs"


def test_import_and_construct_does_not_pollute_cwd(tmp_path):
    """End-to-end: importing core + constructing BotConfig from an arbitrary CWD
    must not create ./data or ./logs there. Run in a subprocess so the import-time
    side effect (cached in sys.modules within one pytest run) is exercised fresh."""
    driver = (
        "import os, sys\n"
        f"os.chdir({str(tmp_path)!r})\n"
        "import core.config\n"
        "core.config.BotConfig()\n"
        "print('LISTING:' + ','.join(sorted(os.listdir('.'))))\n"
    )
    env = {"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"}
    # Reuse the running interpreter (venv) so deps resolve.
    res = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(tmp_path),
        env={**env},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    assert not (tmp_path / "data").exists(), "BotConfig polluted CWD with ./data"
    assert not (tmp_path / "logs").exists(), "import core polluted CWD with ./logs"


def test_config_data_dir_resolves_under_base_dir(tmp_path, monkeypatch):
    """A1: after construction the data_dir is absolute and under base_dir."""
    monkeypatch.chdir(tmp_path)
    from core.config import BotConfig

    cfg = BotConfig()
    assert Path(cfg.data_dir).is_absolute()
    assert str(cfg.data_dir).startswith(cfg.base_dir)
    assert not (tmp_path / "data").exists()
