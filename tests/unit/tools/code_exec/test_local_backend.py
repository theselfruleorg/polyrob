"""Item 3 — LocalSubprocessBackend behaviour + security properties."""
import os

import pytest

from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend
from tools.code_exec.result import ExecutionRequest


@pytest.mark.asyncio
async def test_python_hello_world():
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="python", code="print('hello')"))
    assert r.exit_code == 0
    assert "hello" in r.stdout
    assert not r.timed_out


@pytest.mark.asyncio
async def test_bash_hello_world():
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="bash", code="echo hi"))
    assert r.exit_code == 0
    assert "hi" in r.stdout


@pytest.mark.asyncio
async def test_stdin_is_fed():
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="python", code="import sys; print(sys.stdin.read().strip())", stdin="ping"))
    assert "ping" in r.stdout


@pytest.mark.asyncio
async def test_timeout_kills_infinite_loop():
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="python", code="while True: pass", timeout=1))
    assert r.timed_out is True


@pytest.mark.asyncio
async def test_output_is_capped(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "100")
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="python", code="print('x' * 100000)"))
    assert r.truncated is True
    assert len(r.stdout) < 1000  # capped to ~100 + truncation note


@pytest.mark.asyncio
async def test_api_key_not_in_child_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "nope")
    b = LocalSubprocessBackend()
    code = "import os; print('OPENAI_API_KEY' in os.environ, 'SOME_SECRET_TOKEN' in os.environ)"
    r = await b.run(ExecutionRequest(language="python", code=code))
    assert "False False" in r.stdout


@pytest.mark.asyncio
async def test_artifacts_land_in_given_workdir(tmp_path):
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(
        language="python",
        code="open('artifact.txt','w').write('made it')",
        workdir=str(tmp_path),
    ))
    assert r.exit_code == 0
    assert (tmp_path / "artifact.txt").read_text() == "made it"


@pytest.mark.asyncio
async def test_unsupported_language():
    b = LocalSubprocessBackend()
    r = await b.run(ExecutionRequest(language="ruby", code="puts 1"))
    assert r.exit_code == 2
    assert "unsupported" in r.stderr
