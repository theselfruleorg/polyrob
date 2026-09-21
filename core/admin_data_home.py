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
``goals.db``/``cron.db``/``memory.db`` under a legacy checkout-local ``.polyrob/`` on that
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

core-tier: stdlib + ``core.runtime_paths``/``core.instance`` only (both core).
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


class DeployedEnvUnreadable(AmbiguousDataHome):
    """The deployment's env file EXISTS but this process cannot read it.

    The prod shape (2026-09-20 07:32Z): ``/etc/polyrob/polyrob.env`` is root
    0600 because it holds secrets, and an owner verb run as the service user
    under ``sudo`` gets EACCES. Reading that as "nothing declared" made
    ``admin_instance_id`` fall back to the ``polyrob`` default and write a
    promoted owner-facts doc into ``identity/polyrob/…`` — a tree the running
    service never reads — while reporting "no pending doc". Unreadable is
    "cannot tell", so the verb refuses and names the remedy; CLI seams re-raise
    it as a ``click.ClickException``.
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


def deployed_env_value(key: str) -> Optional[str]:
    """One key's value as the deployment's env file declares it, or None.

    Last assignment wins (shell semantics). Tolerates ``export`` prefixes and
    quoting, and never partial-matches a longer key. Any read/parse fault answers
    None — an unreadable file is "cannot determine", which the caller turns into a
    refusal or a fallback, never a guess.

    The file holds the instance's secrets: it is parsed for the requested key
    only, and is never logged or echoed.
    """
    try:
        with open(DEPLOYED_ENV_FILE, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except PermissionError:
        # The file is there and this euid may not read it: NOT "undeclared".
        raise DeployedEnvUnreadable(_unreadable_remedy(key))
    except OSError:
        return None
    found: Optional[str] = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        name, sep, value = s.partition("=")
        if not sep or name.strip() != key:
            continue
        value = value.strip().strip('"').strip("'").strip()
        if value:
            found = value
    return found


def _unreadable_remedy(key: str) -> str:
    return (f"{DEPLOYED_ENV_FILE} exists but is not readable by this user, so {key} "
            f"cannot be read from the deployment and will NOT be guessed. Pass the "
            f"service's identity explicitly: sudo -u polyrob-agent env "
            f"POLYROB_DATA_DIR=<data home> POLYROB_INSTANCE_ID=<id> "
            f"POLYROB_OWNER_USER_ID=<owner> polyrob … (values from that env file).")


def _deployed_data_dir() -> Optional[str]:
    """``POLYROB_DATA_DIR`` as the deployment's env file declares it, or None."""
    return deployed_env_value("POLYROB_DATA_DIR")


# --- 035 P0-3: the OTHER two axes of an owner-control verb's scope -----------
#
# 031 fixed the data home and left the instance id and the owner tenant reading
# the process environment. On the production box, where systemd exports them from
# the env file but an owner's SSH shell does not, `polyrob owner pending`
# therefore resolved the RIGHT home under the WRONG instance/tenant and printed a
# confident "no pending proposals" over four real ones — beneath a note assuring
# the owner it had used the deployed home. Same class of defect, same seam, same
# rule: adopt what the running service reads, or fall back; never a confident
# wrong answer.
#
# Precedence mirrors `resolve_admin_data_home`: an explicit shell value wins
# (including an active profile's, which pins the env), then the deployment's own
# declaration, then today's resolution unchanged. A box with nothing deployed is
# byte-identical and silent.


def _shell_has(*keys: str) -> bool:
    return any((os.environ.get(k) or "").strip() for k in keys)


def admin_instance_id() -> str:
    """The instance id an owner-control verb must act on."""
    from core.instance import is_safe_tenant_id, resolve_instance_id
    if _shell_has("POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID", "POLYROB_PROFILE"):
        return resolve_instance_id()
    for key in ("POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID"):
        declared = (deployed_env_value(key) or "").strip()
        # An unsafe id is REFUSED, never rewritten (the profile-name rule): it
        # would resolve a path outside the identity tier.
        if declared and is_safe_tenant_id(declared):
            return declared
    return resolve_instance_id()


def admin_owner_principal() -> str:
    """The owner tenant an owner-control verb must act on.

    Never ``None``: the fallback is ``resolve_owner_user_id``, which always
    resolves. A caller needs no ``or "local"`` guard.

    The deployed-env-file lookup is what this function is FOR: an owner's SSH
    shell carries none of the service's environment, so a box where systemd
    exports ``POLYROB_OWNER_USER_ID`` must still be read under that tenant.

    ⚠️ It no longer falls back to the ADOPTED INSTANCE id. That branch made
    ``polyrob owner …`` answer ``rob`` on a box whose REPL, goals and memory all
    answered ``local`` — an instance id names the identity-doc tier and the
    avatar, not an owner tenant. With nothing declared anywhere the answer is
    ``core.instance.resolve_owner_user_id``'s, the same one every other seat
    reads (see ``docs/guide/upgrading.md``, 2026-09-15).
    """
    from core.instance import is_safe_tenant_id, resolve_owner_user_id
    if _shell_has("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID"):
        return resolve_owner_user_id()
    for key in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID"):
        declared = (deployed_env_value(key) or "").strip()
        if declared and is_safe_tenant_id(declared):
            return declared
    return resolve_owner_user_id()


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
