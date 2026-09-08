"""032 lift: the ONE hardening list. The docker backend must emit byte-identical
flags through it, and the app-service variant only changes the mount + workdir."""
from core.container_hardening import hardening_flags


def test_defaults_are_the_sandbox_shape():
    flags = hardening_flags(network="none", workdir_host="/w", pids_limit=128, memory_mb=512,
                            shm_size_mb=64, cpus=1.0, user="65534:65534")
    assert flags == [
        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp", "--pids-limit", "128", "--memory", "512m",
        "--memory-swap", "512m", "--shm-size", "64m", "--cpus", "1.0", "--user", "65534:65534",
        "-v", "/w:/workspace", "-w", "/workspace",
    ]


def test_install_mount_and_app_variant():
    dev = hardening_flags(network="bridge", workdir_host="/w", install_host="/w/.pylibs",
                          pids_limit=1, memory_mb=1, shm_size_mb=1, cpus=0.5, user="u")
    assert dev[dev.index("-v") + 1] == "/w:/workspace"
    assert "/w/.pylibs:/install" in dev and dev[-2:] == ["-w", "/workspace"]
    app = hardening_flags(network="polyrob-app-u1-st", workdir_host="/snap", pids_limit=256,
                          memory_mb=512, shm_size_mb=64, cpus=0.5, user="65534:65534",
                          mount_target="/app", read_only_mount=True, workdir="/app")
    assert "/snap:/app:ro" in app and app[-2:] == ["-w", "/app"]
    assert "--read-only" in app and app[app.index("--cap-drop") + 1] == "ALL"


def test_docker_backend_delegates_byte_identically():
    from tools.code_exec.backends.docker import DockerBackend

    async def _runner(args, *, input=None, timeout=None):
        return (0, "", "")

    b = DockerBackend(docker_runner=_runner)
    via_backend = b._hardening_flags(network="none", workdir_host="/w")
    direct = hardening_flags(network="none", workdir_host="/w", pids_limit=b.pids_limit,
                             memory_mb=b.memory_mb, shm_size_mb=b.shm_size_mb, cpus=b.cpus,
                             user=b.user)
    assert via_backend == direct
