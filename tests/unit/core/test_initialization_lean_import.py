"""058 lean core — ``core.initialization`` must import on a venv WITHOUT the
optional provider SDKs.

Why: the 2026-09-21 20:31Z prod deploy ABORTED at the DB-migration step —
``migrations/migrate.py`` imports ``core.initialization``, which eagerly pulled
``GeminiClient`` through the lazy ``modules.llm`` package, and
``gemini_client.py`` imports ``google.generativeai`` at module load. The prod
extras (``server,browser,crypto,…``) do not carry ``[gemini]``, so the migration
crashed with ``ModuleNotFoundError`` and the deploy rolled back — every seat that
imports the bootstrap (migrations, ``polyrob doctor``, the API lifespan) was one
missing extra away from the same crash. The deploy's own import-test did not
catch it because it exercises the CLI container and the telegram surface, not
the bootstrap module.

The check runs in a SUBPROCESS: blocking a module in ``sys.modules`` only works
before anything has imported it, and the test process has.
"""
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

_BLOCKED = ("google.generativeai", "google", "anthropic")

_PROBE = f"""
import sys
for name in {_BLOCKED!r}:
    sys.modules[name] = None  # makes `import <name>` raise ImportError
import core.initialization as init
assert hasattr(init, "initialize_core"), "bootstrap entry point missing"
print("lean-import-ok")
"""


def test_bootstrap_imports_without_the_optional_provider_sdks():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "core.initialization must not eager-import an optional provider SDK\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}"
    )
    assert "lean-import-ok" in proc.stdout
