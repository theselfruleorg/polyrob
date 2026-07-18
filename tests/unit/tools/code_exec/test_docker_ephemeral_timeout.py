"""Regression (P0): the ephemeral DockerBackend timeout SIGKILLed the local
`docker run` CLI, which does NOT stop the container on the daemon — and the
ephemeral argv carried no --name/--label, so the still-running container was
neither `docker rm -f`-able nor `reap_orphans()`-sweepable. The fix names+labels
the container and wraps the in-container command with `timeout --signal=KILL <n>`
(mirroring the persistent path) so the container self-terminates. Pure argv shape
assertions — no Docker daemon required.
"""
from tools.code_exec.backends.docker import DockerBackend, _SANDBOX_LABEL
from tools.code_exec.result import ExecutionRequest


def test_ephemeral_argv_named_labeled_and_timeout_wrapped():
    b = DockerBackend()
    argv = b._build_run_argv(
        ExecutionRequest(language="python", code="print(1)"),
        "/tmp/ws", container_name="polyrob-sbx-abc", timeout_sec=30.0,
    )
    assert argv[argv.index("--name") + 1] == "polyrob-sbx-abc"
    assert _SANDBOX_LABEL in argv, "container must carry the reap_orphans label"
    ti = argv.index("timeout")
    assert argv[ti:ti + 3] == ["timeout", "--signal=KILL", "30.0"]
    assert argv[ti + 3] == "python", "timeout must wrap the interpreter command"


def test_bash_command_also_timeout_wrapped():
    b = DockerBackend()
    argv = b._build_run_argv(
        ExecutionRequest(language="bash", code="echo hi"),
        "/tmp/ws", container_name="polyrob-sbx-xyz", timeout_sec=10.0,
    )
    ti = argv.index("timeout")
    assert argv[ti:ti + 3] == ["timeout", "--signal=KILL", "10.0"]
    assert argv[ti + 3] == "bash"


def test_default_argv_unchanged_when_no_name_or_timeout():
    # The pure argv path (used by existing shape tests) stays byte-identical.
    b = DockerBackend()
    argv = b._build_run_argv(ExecutionRequest(language="python", code="print(1)"), "/tmp/ws")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--name" not in argv
    assert "timeout" not in argv
    assert argv[-4:] == ["python", "-I", "-c", "print(1)"]
