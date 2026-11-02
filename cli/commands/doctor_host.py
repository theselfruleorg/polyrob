"""Host-readiness lines for ``polyrob doctor``: the things a managed user-mode
install (``~/.polyrob``, a ``systemctl --user`` unit) needed root work for.

Each checker returns a list of report lines and never raises; ``doctor_report``
extends its output with :func:`host_readiness_lines`. A line that starts with
``CRITICAL`` names a state where the agent runs on the wrong data or cannot do
its work. Kept out of ``doctor.py``, which sits at the file-size ceiling.
"""
from __future__ import annotations

from typing import Callable, List

Checker = Callable[[dict], List[str]]


def _checkers() -> List[Checker]:
    from cli.commands.doctor_data_home import data_home_split_lines
    from cli.commands.doctor_sandbox import docker_sandbox_lines
    from cli.commands.doctor_env_copies import env_copy_lines
    from cli.commands.doctor_apps import apps_publish_lines
    return [data_home_split_lines, docker_sandbox_lines, env_copy_lines, apps_publish_lines]


def host_readiness_lines(env: dict) -> List[str]:
    out: List[str] = []
    for check in _checkers():
        try:
            out.extend(check(env) or [])
        except Exception as exc:  # noqa: BLE001 — a probe never fails doctor
            out.append(f"host check {getattr(check, '__name__', '?')}: unknown ({type(exc).__name__})")
    return out
