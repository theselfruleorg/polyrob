"""The ONE skip rule for tests that drive a real Docker daemon.

A test that needs the daemon runs when ``docker info`` answers. On a dev or
release machine with the CLI installed but no daemon running, it SKIPS with
the reason, instead of failing as if the sandbox code were broken.

On CI (``CI`` set, as GitHub Actions does) a stopped daemon is NOT a skip: the
containment tests must run there, so the test runs and fails loudly. Only a
host with no ``docker`` CLI at all skips there (the rule before this module).
"""
import functools
import os
import shutil
import subprocess

import pytest


@functools.lru_cache(maxsize=1)
def docker_daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _skip_reason() -> str:
    if shutil.which("docker") is None:
        return "docker not installed"
    return "docker daemon not running (start it to run the sandbox tests)"


def _skip() -> bool:
    if shutil.which("docker") is None:
        return True
    return not os.environ.get("CI") and not docker_daemon_up()


needs_docker = pytest.mark.skipif(_skip(), reason=_skip_reason())
