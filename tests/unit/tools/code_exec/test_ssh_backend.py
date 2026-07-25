"""T2.7 Task 1 — SshBackend: pure argv/quoting + fake-runner behaviour + registry wiring.

No real ``ssh`` binary or network is required for any test here — every behavioural
test injects a fake runner (mirrors ``test_docker_persistent.py``'s ``_FakeDocker``
pattern) and every argv/quoting test calls the PURE ``_build_ssh_argv``/
``_build_remote_command`` helpers directly (mirrors ``test_docker_backend.py``'s
``_argv`` pure-argv pattern). A real live-host smoke test would be gated
``skipif`` on ``CODE_EXEC_SSH_TEST_HOST`` (not added here — out of scope for Task 1,
which is hostless by design).

Quoting note (the decision this task left to the implementer, empirically verified
against a real local ``sshd`` — see the T2.7 Task-1 report): the remote command is
built as ONE argv-trailing string via ``shlex.quote`` (POSIX-safe single-quoting),
never passed through a local shell (``subprocess`` gets an argv list, never
``shell=True``), so local expansion never happens; and OpenSSH's own client strips a
literal ``--`` token from the tail of its argv (even when placed AFTER the
destination) before joining the remainder into the command line it sends to the
remote shell — confirmed empirically, not just by reading the man page synopsis.
"""
from __future__ import annotations

import logging
import shlex

import pytest

from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends.ssh import SshBackend
from tools.code_exec.result import ExecutionRequest


class _FakeSsh:
    """Records every call; returns a canned (exit_code, stdout, stderr) or raises
    the module's internal timeout signal, mirroring ``_FakeDocker``."""

    def __init__(self, *, exit_code=0, stdout="", stderr="", raise_timeout=False):
        self.calls = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._raise_timeout = raise_timeout

    async def __call__(self, args, *, input=None, timeout=None):
        self.calls.append({"args": list(args), "input": input, "timeout": timeout})
        if self._raise_timeout:
            from tools.code_exec.backends.ssh import _SshRunnerTimeout
            raise _SshRunnerTimeout("simulated timeout")
        return self._exit_code, self._stdout, self._stderr


# --------------------------------------------------------------------------
# 1. Pure argv shape — host/user/port/key/BatchMode, no-user and no-key variants
# --------------------------------------------------------------------------

def test_argv_has_expected_ssh_flags(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setenv("CODE_EXEC_SSH_USER", "deploy")
    monkeypatch.setenv("CODE_EXEC_SSH_PORT", "2222")
    monkeypatch.setenv("CODE_EXEC_SSH_KEY", "/tmp/id_ed25519")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="python", code="print(1)"))

    assert argv[0] == "ssh"
    assert argv[1:5] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == "/tmp/id_ed25519"
    assert "deploy@example.com" in argv
    target_idx = argv.index("deploy@example.com")
    # SECURITY (review fix, T2.7): `--` must come BEFORE target, never after —
    # see the module docstring's "Option-injection safety" note. `--` after
    # target is too late: OpenSSH has already parsed target as an option by then.
    assert argv[target_idx - 1] == "--"
    assert argv[-1] == argv[target_idx + 1]  # the wrapped command is the LAST element


def test_no_user_omits_at_sign(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.delenv("CODE_EXEC_SSH_USER", raising=False)
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="bash", code="true"))
    assert "example.com" in argv
    assert not any(a.endswith("@example.com") for a in argv)


def test_no_key_omits_dash_i(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.delenv("CODE_EXEC_SSH_KEY", raising=False)
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="bash", code="true"))
    assert "-i" not in argv


def test_timeout_wrapper_present_in_remote_command(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="bash", code="true", timeout=7))
    remote_cmd = argv[-1]
    assert remote_cmd.startswith("timeout --signal=KILL 7")


# --------------------------------------------------------------------------
# 1b. Option-injection guard — a `-`-prefixed host/user must never reach ssh
#     as an unescaped destination argument (review finding, T2.7).
# --------------------------------------------------------------------------

def test_dash_prefixed_host_rejected_at_construction(monkeypatch):
    """CODE_EXEC_SSH_HOST looking like an ssh flag (e.g. a smuggled
    -oProxyCommand=...) must be refused when the argv is built — belt-and-
    suspenders defense-in-depth alongside the `--`-before-target placement."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "-oProxyCommand=touch /tmp/pwn")
    b = SshBackend()
    with pytest.raises(ExecutionBackendError):
        b._build_ssh_argv(ExecutionRequest(language="python", code="print(1)"))


def test_dash_prefixed_user_rejected_at_construction(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setenv("CODE_EXEC_SSH_USER", "-oProxyCommand=touch /tmp/pwn")
    b = SshBackend()
    with pytest.raises(ExecutionBackendError):
        b._build_ssh_argv(ExecutionRequest(language="python", code="print(1)"))


@pytest.mark.asyncio
async def test_run_with_dash_prefixed_host_returns_error_without_touching_runner(monkeypatch):
    """Even if a caller only ever calls run() (never _build_ssh_argv directly),
    a malicious/misconfigured host is rejected as a structured ExecutionResult
    — the runner (the local `ssh` CLI) is never invoked."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "-oProxyCommand=touch /tmp/pwn")
    fake = _FakeSsh()
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="python", code="print(1)"))
    assert r.exit_code == 2
    assert "begins with '-'" in r.stderr
    assert fake.calls == []


def test_normal_host_still_produces_a_working_argv(monkeypatch):
    """Sanity/regression check: a well-formed host is NOT caught by the
    option-injection guard and the resulting argv keeps the safe `-- target
    command` shape (see test_argv_has_expected_ssh_flags for the full shape
    assertion)."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setenv("CODE_EXEC_SSH_USER", "deploy")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="python", code="print(1)"))
    assert argv[0] == "ssh"
    dd_idx = argv.index("--")
    assert argv[dd_idx + 1] == "deploy@example.com"
    assert argv[dd_idx + 2] == argv[-1]


# --------------------------------------------------------------------------
# 2. python vs bash command construction + quoting honesty
# --------------------------------------------------------------------------

def test_python_uses_python3_dash_c(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="python", code="print(1)"))
    assert "python3 -c " in argv[-1]


def test_bash_uses_bash_dash_c(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="bash", code="echo hi"))
    assert "bash -c " in argv[-1]


@pytest.mark.parametrize("code", [
    "print('hello')",
    'print("hello")',
    "print('it\\'s a test')",
    "line1\nline2\nprint(1)",
    'print("$(whoami)")',
    "print('`echo pwned`')",
    "import os; os.system('echo hi')",
])
def test_python_code_survives_quoting_round_trip(monkeypatch, code):
    """The code must arrive at the interpreter LITERALLY — never expanded locally
    (no shell=True is ever used) or remotely outside python3 -c's own string arg
    (single-quoted per shlex.quote, so $()/`` /"" inside are inert to the shell)."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="python", code=code))
    remote_cmd = argv[-1]
    tokens = shlex.split(remote_cmd, posix=True)
    assert tokens[-1] == code


@pytest.mark.parametrize("code", [
    "echo 'hello'",
    'echo "hello"',
    "echo it\\'s ok",
    "echo one\necho two",
    'echo "$(whoami)"',
    "echo '`id`'",
])
def test_bash_code_survives_quoting_round_trip(monkeypatch, code):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend()
    argv = b._build_ssh_argv(ExecutionRequest(language="bash", code=code))
    remote_cmd = argv[-1]
    tokens = shlex.split(remote_cmd, posix=True)
    assert tokens[-1] == code


# --------------------------------------------------------------------------
# 3. Fake-runner behaviour: success / exit-code / timeout / output cap / env scrub
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_success_with_fake_runner(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(exit_code=0, stdout="hello\n", stderr="")
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="python", code="print('hello')"))
    assert r.exit_code == 0
    assert "hello" in r.stdout
    assert r.timed_out is False
    assert len(fake.calls) == 1
    assert fake.calls[0]["args"][0] == "ssh"


@pytest.mark.asyncio
async def test_run_nonzero_exit_code_passed_through(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(exit_code=7, stdout="", stderr="boom")
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="bash", code="exit 7"))
    assert r.exit_code == 7
    assert "boom" in r.stderr
    assert r.timed_out is False


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [124, 137])
async def test_timeout_exit_codes_mapped_to_timed_out(monkeypatch, exit_code):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(exit_code=exit_code, stdout="", stderr="")
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="python", code="while True: pass", timeout=1))
    assert r.timed_out is True
    assert r.exit_code == exit_code


@pytest.mark.asyncio
async def test_runner_timeout_exception_maps_to_timed_out(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(raise_timeout=True)
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="bash", code="sleep 100", timeout=1))
    assert r.timed_out is True


@pytest.mark.asyncio
async def test_output_is_capped(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "50")
    fake = _FakeSsh(exit_code=0, stdout="x" * 10000, stderr="")
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="python", code="print('x' * 10000)"))
    assert r.truncated is True
    assert len(r.stdout) < 500


@pytest.mark.asyncio
async def test_env_scrubbed_from_remote_command(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(exit_code=0)
    b = SshBackend(ssh_runner=fake)
    await b.run(ExecutionRequest(
        language="python", code="print(1)",
        env={"FOO": "bar", "MY_API_KEY": "sk-secret-value"},
    ))
    remote_cmd = fake.calls[0]["args"][-1]
    assert "FOO=bar" in remote_cmd
    assert "MY_API_KEY" not in remote_cmd
    assert "sk-secret-value" not in remote_cmd


@pytest.mark.asyncio
async def test_invalid_env_key_rejected(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh(exit_code=0)
    b = SshBackend(ssh_runner=fake)
    await b.run(ExecutionRequest(
        language="python", code="print(1)",
        env={"1BAD": "x", "bad-name": "y", "OK_NAME": "z"},
    ))
    remote_cmd = fake.calls[0]["args"][-1]
    assert "1BAD" not in remote_cmd
    assert "bad-name" not in remote_cmd
    assert "OK_NAME=z" in remote_cmd


@pytest.mark.asyncio
async def test_unsupported_language_returns_error_without_touching_runner(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh()
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="ruby", code="puts 1"))
    assert r.exit_code == 2
    assert "unsupported" in r.stderr
    assert fake.calls == []


@pytest.mark.asyncio
async def test_run_without_host_returns_error_without_touching_runner(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_SSH_HOST", raising=False)
    fake = _FakeSsh()
    b = SshBackend(ssh_runner=fake)
    r = await b.run(ExecutionRequest(language="bash", code="echo hi"))
    assert r.exit_code == 2
    assert fake.calls == []


# --------------------------------------------------------------------------
# 4. capabilities: sandbox False by default, attestation flips it True
# --------------------------------------------------------------------------

def test_capabilities_default_sandbox_false(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_SSH_SANDBOXED", raising=False)
    b = SshBackend()
    caps = b.capabilities
    assert caps["sandbox"] is False
    assert caps["isolation"] == "remote-host"
    assert caps["network"] is True


def test_capabilities_sandboxed_attestation_flips_true(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_SANDBOXED", "true")
    b = SshBackend()
    assert b.capabilities["sandbox"] is True


# --------------------------------------------------------------------------
# 5. setup(): fails loudly without CODE_EXEC_SSH_HOST; registry create() zero-arg
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_fails_loudly_without_host(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_SSH_HOST", raising=False)
    b = SshBackend(ssh_runner=_FakeSsh())
    with pytest.raises(ExecutionBackendError):
        await b.setup()


@pytest.mark.asyncio
async def test_setup_succeeds_with_host_and_injected_runner_even_without_real_ssh_binary(monkeypatch):
    """An injected runner is used for every real invocation, so setup() must not
    require the real 'ssh' CLI on PATH in that case — the PATH check is only
    meaningful (and only applied) for the DEFAULT runner."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setattr("tools.code_exec.backends.ssh.shutil.which", lambda _: None)
    b = SshBackend(ssh_runner=_FakeSsh())
    await b.setup()  # must not raise


@pytest.mark.asyncio
async def test_setup_no_live_connection_probe(monkeypatch):
    """setup() must never invoke the runner — it only verifies local
    prerequisites (host configured, binary present), never a live ssh call."""
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    fake = _FakeSsh()
    b = SshBackend(ssh_runner=fake)
    await b.setup()
    assert fake.calls == []


def test_registry_create_ssh_is_zero_arg_constructible():
    from tools.code_exec import default_registry
    backend = default_registry.create("ssh")
    assert isinstance(backend, SshBackend)
    assert backend.name == "ssh"


@pytest.mark.asyncio
async def test_teardown_is_a_noop(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    b = SshBackend(ssh_runner=_FakeSsh())
    await b.teardown()  # must not raise


# --------------------------------------------------------------------------
# 6. key path never leaks into log output
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_key_path_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "example.com")
    monkeypatch.setenv("CODE_EXEC_SSH_KEY", "/super/secret/path/id_ed25519")
    fake = _FakeSsh(exit_code=0, stdout="ok")
    b = SshBackend(ssh_runner=fake)
    with caplog.at_level(logging.DEBUG):
        await b.run(ExecutionRequest(language="bash", code="echo ok"))
    for record in caplog.records:
        assert "/super/secret/path/id_ed25519" not in record.getMessage()
        assert "id_ed25519" not in record.getMessage()
