"""Sudo on the host shell (073 W5).

Default (``SHELL_HOST_SUDO=refuse``): a host command that runs ``sudo``/``doas``/
``su``/``pkexec`` is refused — the agent runs as its own user by design.

``SHELL_HOST_SUDO=prompt``: a FOREGROUND host command that runs ``sudo`` asks the
owner for the password on the controlling terminal, once per command:

- only on the host executor and only when the turn's seat is the terminal
  (``core.security.host_execution.host_seat_allowed``) — never a chat seat;
- the password is read with ``getpass`` from ``/dev/tty``; it is never cached,
  never put in an env var, never logged, never written to a file;
- the command is rewritten so each ``sudo`` at a command position reads the
  password from stdin (``sudo -S -p ''``) and the password reaches the command
  ONLY as stdin bytes;
- no terminal (a service, a pipe, a CI run) -> refused. An empty answer -> refused.
- ``doas``/``su``/``pkexec`` stay refused (only ``sudo`` has a stdin password mode
  this can drive safely).

The mode is frozen at import, like the posture and the approval provider (an
agent's own ``export`` never reaches this process's environment anyway).

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import os
import re
from typing import Optional

SUDO_MODES = ("refuse", "prompt")


def _read_mode() -> str:
    raw = (os.getenv("SHELL_HOST_SUDO") or "refuse").strip().lower()
    return raw if raw in SUDO_MODES else "refuse"


_MODE = _read_mode()


def host_sudo_mode() -> str:
    """``SHELL_HOST_SUDO`` (frozen at import): ``refuse`` (default) | ``prompt``.
    Garbage -> ``refuse``."""
    return _MODE


def _refreeze_for_tests() -> None:
    global _MODE
    _MODE = _read_mode()


#: ``sudo`` at a command position: line start, after ; & | ( ` or $( , and after
#: the shell keywords that start a command.
_SUDO_AT_COMMAND = re.compile(
    r"(?P<lead>(?:^|[;&|(`\n]|\$\(|\b(?:then|do|else|elif|time|!)\s)\s*)sudo(?=\s|$)"
)
_OTHER_ESCALATION = re.compile(r"(^|[;&|(`\s])(doas|su|pkexec)(\s|$)")


def other_escalation(command: str) -> bool:
    """True when the command uses an escalation tool other than ``sudo``."""
    return bool(_OTHER_ESCALATION.search(command or ""))


def rewrite_sudo(command: str) -> str:
    """Make every command-position ``sudo`` read its password from stdin, with no
    prompt text in the output: ``sudo`` -> ``sudo -S -p ''``."""
    return _SUDO_AT_COMMAND.sub(lambda m: f"{m.group('lead')}sudo -S -p ''", command or "")


def tty_available() -> bool:
    """True when this process has a controlling terminal to ask on."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def prompt_password(command: str) -> Optional[str]:
    """Ask the owner on ``/dev/tty``. None = no terminal, or the owner declined
    (empty answer / Ctrl-C / EOF). Blocking: call it off the event loop."""
    if not tty_available():
        return None
    import getpass
    shown = " ".join((command or "").split())
    if len(shown) > 300:
        shown = shown[:300] + " ..."
    try:
        with open("/dev/tty", "w") as tty:
            tty.write("\n[polyrob] The agent wants to run this host command with sudo:\n"
                      f"  {shown}\n"
                      "Type your sudo password to allow it once (empty = refuse). "
                      "It is not stored.\n")
            tty.flush()
        pw = getpass.getpass("sudo password: ")
    except (EOFError, KeyboardInterrupt, OSError):
        return None
    return pw or None


__all__ = ["host_sudo_mode", "rewrite_sudo", "other_escalation", "tty_available",
           "prompt_password", "SUDO_MODES"]
