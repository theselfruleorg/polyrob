"""Which data home does an OWNER-CONTROL verb act on? (031)

``core.runtime_paths.resolve_data_home()`` deliberately never applies the server
default ``/var/lib/polyrob``: a headless deploy is expected to set
``POLYROB_DATA_DIR`` explicitly. That expectation holds for the DAEMON — systemd
exports it from ``EnvironmentFile=/etc/polyrob/polyrob.env`` — and breaks for the
OWNER standing in an SSH shell, where the variable is simply absent and the
resolution falls through to ``cwd/.polyrob``. So ``polyrob autonomy pause`` run
from ``~`` on the production box wrote ``AUTONOMY_PAUSE.json`` into
``/root/.polyrob/``, read it back from that same wrong place, and printed a
confident "⏸ Paused everything" the running daemon never saw. Stray
``goals.db``/``cron.db``/``memory.db`` under ``/root/rob_dev/.polyrob/`` on that
box are the same accident with a different file name.

031's contract is that every seat reports the VERIFIED state, so an owner-control
verb must never print a success line for a write the daemon cannot see. This
module is the ONE detector both CLI groups (``polyrob autonomy``, ``polyrob
owner``) use; it answers in one of four ways:

* ``POLYROB_DATA_DIR`` is set in the environment -> use it. Unchanged, silent.
* it is not set, but a deployed instance is detectable AND names its own data
  home -> ADOPT that home, with a note saying which one and why.
* it is not set, a deployed instance is detectable, but its home cannot be read
  -> REFUSE (``AmbiguousDataHome``), naming both candidate paths and the fix.
* nothing is deployed on this box -> today's local resolution, byte-identical
  and silent. A developer machine sees no new warning noise.

The deployment env file is parsed for exactly ONE key and is never echoed: it
holds the instance's secrets.

core-tier: stdlib + ``core.runtime_paths`` only.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The env file a systemd deployment loads (``EnvironmentFile=``). Read-only, and
#: only ever parsed for ``POLYROB_DATA_DIR`` — never logged, never echoed.
DEPLOYED_ENV_FILE = "/etc/polyrob/polyrob.env"

#: Where an installed unit would live. Probed with a cheap glob (no subprocess:
#: ``systemctl`` costs a fork and is absent on a developer's macOS box anyway).
UNIT_DIRS: Tuple[str, ...] = (
    "/etc/systemd/system",
    "/lib/systemd/system",
    "/usr/lib/systemd/system",
)

_UNIT_GLOB = "polyrob*.service"

#: Adoption notes are per-process-once, keyed by the adopted path: `polyrob owner
#: pending` calls the seam twice in one command and must not say it twice.
_NOTED: set = set()


class AmbiguousDataHome(RuntimeError):
    """This box has a deployed instance whose data home cannot be determined.

    Raised instead of silently acting on the shell-local path. The message names
    both candidates and the one-line fix; CLI seams re-raise it as a
    ``click.ClickException``.
    """


@dataclass(frozen=True)
class DataHomeResolution:
    #: the home to act on ("" only when *ambiguous*)
    path: str
    #: "env" | "deployed" | "local"
    source: str
    #: what ``resolve_data_home()`` would have returned on its own
    local_path: str
    #: the deployed instance's home, when it could be read
    deployed_path: Optional[str]
    #: True = refuse; the caller must not proceed
    ambiguous: bool
    #: adoption note (source="deployed") or the refusal text (ambiguous)
    message: str


def reset_admin_data_home_notes() -> None:
    """Forget which adoption notes were already shown (tests / long-lived hosts)."""
    _NOTED.clear()


def _local_home() -> str:
    from core.runtime_paths import resolve_data_home
    return str(resolve_data_home())


def _deployed_data_dir() -> Optional[str]:
    """``POLYROB_DATA_DIR`` as the deployment's env file declares it, or None.

    Last assignment wins (shell semantics). Tolerates ``export`` prefixes and
    quoting. Any read/parse fault answers None — an unreadable file is "cannot
    determine", which the caller turns into a refusal, never a guess.
    """
    try:
        with open(DEPLOYED_ENV_FILE, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    found: Optional[str] = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if not s.startswith("POLYROB_DATA_DIR"):
            continue
        key, _, value = s.partition("=")
        if key.strip() != "POLYROB_DATA_DIR":
            continue
        value = value.strip().strip('"').strip("'").strip()
        if value:
            found = value
    return found


def _deployment_evidence() -> List[str]:
    """Human-readable proof that a polyrob instance is deployed on this box."""
    evidence: List[str] = []
    try:
        if os.path.exists(DEPLOYED_ENV_FILE):
            evidence.append(DEPLOYED_ENV_FILE)
    except OSError:
        pass
    for base in UNIT_DIRS:
        try:
            for unit in sorted(glob.glob(os.path.join(base, _UNIT_GLOB))):
                evidence.append(os.path.basename(unit))
        except OSError:
            continue
    return evidence


def resolve_admin_data_home() -> DataHomeResolution:
    """Resolve the data home an owner-control verb may act on (see module doc)."""
    local = _local_home()
    env_value = (os.environ.get("POLYROB_DATA_DIR") or "").strip()
    if env_value:
        # The owner (or systemd, or an active profile) pinned it. Byte-identical
        # to the pre-fix behaviour, including the resolve/expand semantics.
        return DataHomeResolution(path=local, source="env", local_path=local,
                                  deployed_path=None, ambiguous=False, message="")

    evidence = _deployment_evidence()
    if not evidence:
        return DataHomeResolution(path=local, source="local", local_path=local,
                                  deployed_path=None, ambiguous=False, message="")

    deployed = _deployed_data_dir()
    if deployed:
        if os.path.abspath(deployed) == os.path.abspath(local):
            return DataHomeResolution(path=local, source="local", local_path=local,
                                      deployed_path=deployed, ambiguous=False,
                                      message="")
        return DataHomeResolution(
            path=deployed, source="deployed", local_path=local,
            deployed_path=deployed, ambiguous=False,
            message=(f"note: POLYROB_DATA_DIR is not set in this shell — using the "
                     f"DEPLOYED data home {deployed} (declared in {DEPLOYED_ENV_FILE}) "
                     f"instead of {local}, because that is the home the running "
                     f"service reads."))

    return DataHomeResolution(
        path="", source="local", local_path=local, deployed_path=None,
        ambiguous=True,
        message=(f"refusing to act on a data home the service may not read: this box "
                 f"has a deployed polyrob instance ({', '.join(evidence)}) but "
                 f"POLYROB_DATA_DIR is not set in this shell, so the write would land "
                 f"in {local} — a path the service never reads, and the confirmation "
                 f"would be a lie. Load the deployment environment first:\n"
                 f"    set -a; . {DEPLOYED_ENV_FILE}; set +a\n"
                 f"or export POLYROB_DATA_DIR=<the service's data home>, then re-run."))


def admin_data_home(echo: Optional[Callable[[str], None]] = None) -> str:
    """The data home for an owner-control verb, or raise :class:`AmbiguousDataHome`.

    *echo* (e.g. ``click.echo``) receives the adoption note once per process per
    adopted path. A raising *echo* never blocks the verb.
    """
    res = resolve_admin_data_home()
    if res.ambiguous:
        raise AmbiguousDataHome(res.message)
    if res.message and echo is not None and res.path not in _NOTED:
        _NOTED.add(res.path)
        try:
            echo(res.message)
        except Exception:
            logger.debug("admin_data_home: note echo failed (non-fatal)", exc_info=True)
    return res.path
