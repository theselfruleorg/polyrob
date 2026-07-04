"""Guard: the core/CLI import path must not pull in FastAPI/Starlette/Uvicorn (R7)."""
import subprocess
import sys
import textwrap


def test_core_imports_without_fastapi():
    code = textwrap.dedent('''
        import builtins
        _real = builtins.__import__
        def _block(name, *a, **k):
            if name.split(".")[0] in {"fastapi", "starlette", "uvicorn"}:
                raise ImportError("blocked server dep: " + name)
            return _real(name, *a, **k)
        builtins.__import__ = _block
        import core.bootstrap            # noqa
        import agents.task_agent_lite     # noqa
        import agents.task.agent.conversation  # noqa
        print("CORE_IMPORTS_OK")
    ''')
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "CORE_IMPORTS_OK" in result.stdout, (
        f"core import path pulled a server dep.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
