"""Boot refusal for an anonymous console on a server-shaped deployment (S8).

`webview/webgate.py::posture()` returns ``"local"`` — *every anonymous request
is the owner*, no login, the full control plane — whenever `POLYROB_POSTURE`,
`WEBGATE_MULTITENANT` and `WEBGATE_HOST`/`WEBVIEW_HOST` are all unset. That
default is right for the loopback primitive on a workstation and catastrophic on
a server: the shipped unit passes `--host 127.0.0.1` on the uvicorn **argv**
(invisible to `posture()`, which only reads env) and nginx proxies the internet
into that loopback socket. Lose `/etc/polyrob/webview.env` and the console
serves pause/resume, goal & cron actions, invoice settle, app approve/kill and
config writes to anyone, with no error anywhere.

So this module reads the signals `posture()` structurally cannot, and REFUSES to
start rather than serving owner powers anonymously:

  - ``WEBVIEW_PUBLIC_URL``  — the console has a public address
  - ``--proxy-headers`` / ``--forwarded-allow-ips`` on the process argv — it
    runs behind a reverse proxy
  - ``POLYROB_DATA_DIR`` pointing OUTSIDE the user's home — a system deployment
    (``/var/lib/polyrob``). A profile's ``~/.polyrob/profiles/<name>/data`` is
    deliberately NOT a signal: refusing there would break
    ``polyrob dashboard -P <profile>`` on a laptop.

Escape hatch: ``WEBVIEW_ALLOW_LOCAL_POSTURE=1`` for an operator who genuinely
fronts the console with their own auth layer. It is honoured loudly (WARNING),
never silently.

Pure + injectable (``env``/``argv``/``posture`` args) so the whole rail is
testable without a process; the lifespan calls it with no arguments.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

ALLOW_FLAG = "WEBVIEW_ALLOW_LOCAL_POSTURE"
_PROXY_ARGS = ("--proxy-headers", "--forwarded-allow-ips")


def _is_system_path(raw: str) -> bool:
    """True when *raw* is not under the current user's home (⇒ a service tree)."""
    try:
        target = Path(raw).expanduser().resolve()
        home = Path.home().resolve()
    except Exception:
        return False
    return home not in target.parents and target != home


def server_signals(env: Optional[Mapping[str, str]] = None,
                   argv: Optional[Sequence[str]] = None) -> list:
    """The observable "this is a server, not a workstation" signals. Never raises."""
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    signals = []
    try:
        if str(env.get("WEBVIEW_PUBLIC_URL") or "").strip():
            signals.append("WEBVIEW_PUBLIC_URL is set — the console has a public address")
        hits = [a for a in argv
                if any(a == p or a.startswith(p + "=") for p in _PROXY_ARGS)]
        if hits:
            signals.append(
                "the process runs behind a reverse proxy (argv: " + " ".join(hits) + ")")
        data_dir = str(env.get("POLYROB_DATA_DIR") or "").strip()
        if data_dir and _is_system_path(data_dir):
            signals.append(
                f"POLYROB_DATA_DIR={data_dir} is a system data directory — a service "
                "deployment, not a workstation")
    except Exception:  # pragma: no cover — a broken probe must not mask the others
        logger.debug("posture signal probe failed", exc_info=True)
    return signals


def _allow_override(env: Mapping[str, str]) -> bool:
    """``WEBVIEW_ALLOW_LOCAL_POSTURE`` with ``bool_env`` semantics.

    Reads the (injectable) mapping rather than calling ``bool_env`` directly so a
    caller-supplied env is honoured, but the truthiness parser is the canonical
    one — never an open-coded ``== "true"``.
    """
    from core.env import parse_bool
    raw = env.get(ALLOW_FLAG)
    if raw is None or str(raw).strip() == "":
        return False          # unset/blank ⇒ the default (False), as bool_env does
    return parse_bool(raw, False)


def assert_console_posture(*, env: Optional[Mapping[str, str]] = None,
                           argv: Optional[Sequence[str]] = None,
                           posture: Optional[str] = None) -> None:
    """Raise ``RuntimeError`` when an anonymous console would serve a server.

    No-op at `own_ops`/`multitenant` posture (both require an owner login), on a
    plain workstation (no server signal), and when ``WEBVIEW_ALLOW_LOCAL_POSTURE``
    is set. Called from the webview startup handler, so the refusal aborts the
    boot: uvicorn reports "Application startup failed" and exits non-zero.
    """
    env = os.environ if env is None else env
    if posture is None:
        from webview import webgate
        posture = webgate.posture()
    if posture != "local":
        return
    signals = server_signals(env, argv)
    if not signals:
        return
    if _allow_override(env):
        logger.warning(
            "console posture is 'local' (no login — every anonymous request is the "
            "owner) on a server-shaped deployment [%s], allowed explicitly by %s=1",
            "; ".join(signals), ALLOW_FLAG)
        return
    message = (
        "REFUSING TO START: the console posture is 'local' — no login, every "
        "anonymous request is treated as the OWNER — but this looks like a server:\n"
        + "".join(f"  - {s}\n" for s in signals)
        + "At 'local' posture the control plane (pause/resume, goals, cron, invoice "
        "settle, app approve/kill, config writes) is reachable without any "
        "credential.\n"
        "Fix: set POLYROB_POSTURE=own_ops (plus POLYROB_OWNER_USERNAME and "
        "POLYROB_OWNER_PASSWORD_HASH) in /etc/polyrob/webview.env, or "
        f"POLYROB_POSTURE=multitenant.\n"
        f"Override (only if you front the console with your own auth): {ALLOW_FLAG}=1."
    )
    logger.critical(message)
    raise RuntimeError(message)


def assert_writable_console(*, env: Optional[Mapping[str, str]] = None,
                            posture: Optional[str] = None) -> None:
    """Refuse to boot a WRITABLE console whose preconditions are not met (043 W7/W8).

    Two of them today — a BOUND OWNER and a SHARED SESSION REGISTRY — and both
    are reported together rather than one per restart: an operator flipping
    ``WEBVIEW_READ_ONLY=false`` wants the whole list, not a game of whack-a-mole
    across three failed boots.

    Each precondition needs the same three conditions to matter:

    - **writable.** A monitoring console that reads the wrong tenant shows too
      little; a console you can DRIVE writes into it, and resumes sessions with
      it. Prod is read-only today — this is the check that has to pass before
      that flag flips.
    - **the precondition is actually unmet** (see the two helpers below).
    - **not a ``local`` posture ON A WORKSTATION.** On a laptop the console and
      the agent share one tree and one default, and ``polyrob dashboard`` is
      usually the only agent process, so refusing would break first run and buy
      nothing. The risk is still SAID once (a warning), because a laptop can also
      be running ``polyrob telegram``.

      ⚠️ "``local``" alone is NOT enough to earn that pass. :func:`assert_console_posture`
      refuses a ``local`` posture on a server — unless the operator set
      ``WEBVIEW_ALLOW_LOCAL_POSTURE=1``, which is precisely the case where a
      ``local`` console IS a server deployment with its own auth layer in front.
      So the workstation pass requires ``local`` AND no
      :func:`server_signals`; a signalled box is held to both preconditions.

    Called from ``server.py::startup_event``, so the refusal aborts the boot:
    uvicorn reports "Application startup failed" and exits non-zero rather than
    serving a console that writes into a tenant nobody chose, or resumes a
    session another process owns.
    """
    from webview import webgate
    env = os.environ if env is None else env
    if webgate.read_only():
        return
    if posture is None:
        posture = webgate.posture()
    # A `local` posture that shows server signals is a SERVER the operator waved
    # through with WEBVIEW_ALLOW_LOCAL_POSTURE — it does not get the workstation
    # pass below.
    local = posture == "local" and not server_signals(env)
    problems = []
    for check in (_owner_problem, _shared_registry_problem):
        problem = check(env, local=local)
        if problem:
            problems.append(problem)
    if not problems:
        return
    message = ("REFUSING TO START: this console is WRITABLE "
               "(WEBVIEW_READ_ONLY is not set) but:\n"
               + "".join(f"  - {p}\n" for p in problems)
               + "Fix the above, or run the console read-only "
                 "(WEBVIEW_READ_ONLY=true) — a read-only console still logs in, "
                 "so the monitoring seat is never lost.")
    logger.critical(message)
    raise RuntimeError(message)


def _owner_problem(env: Mapping[str, str], *, local: bool) -> Optional[str]:
    """The W7 precondition: an owner the console can NAME.

    With none, ``core.instance.resolve_owner_user_id`` falls back to the local
    tenant, so the console scopes every read AND write to ``"local"``: nothing
    errors, the pages render honest-LOOKING empty lists for an agent whose goals,
    invoices and pending items live under the real owner's id, and console writes
    land in that tenant too.

    Console and agent resolve the owner through the SAME function, so an unbound
    PAIR still agrees; the divergence appears when one side is bound and the
    other is not — which a separate ``/etc/polyrob/webview.env`` makes easy.
    """
    from webview import webgate
    if webgate.owner_is_bound(dict(env)):
        return None
    if local:
        return None   # local_owner_id() warns once; see webgate
    from core.instance import resolve_owner_user_id
    # Resolved from the SAME env the binding decision above read, so the message
    # names the tenant this console would actually use.
    return (webgate.UNBOUND_OWNER_MESSAGE + ". Without one the console scopes "
            f"every read AND write to the tenant '{resolve_owner_user_id(dict(env))}' and "
            "renders honest-looking EMPTY lists for the real owner's data. Set "
            "POLYROB_OWNER_USER_ID to the same value the agent service uses.")


#: The ONE sentence for the session-registry precondition (043 W8).
SHARED_REGISTRY_MESSAGE = (
    "a writable console needs SESSION_REGISTRY_BACKEND=sqlite so it cannot "
    "resume a session the agent service owns")


def _shared_registry_problem(env: Mapping[str, str], *,
                             local: bool) -> Optional[str]:
    """The W8 precondition: a session registry the two processes SHARE.

    The console carries its OWN ``TaskAgent`` (``server.py``, preferred over the
    ``:9000`` hop whenever the task router is mounted in-process). With the
    default in-memory registry that agent knows nothing about the sessions
    ``polyrob.service`` owns, so a console message to a LIVE session resumes it
    HERE: two processes stepping the same session, two message histories, one
    workspace. ``SESSION_REGISTRY_BACKEND=sqlite`` is what makes that
    impossible — the session resolves REMOTE and the console answers an honest
    409 with the owning pid (``api/session_routing.py``) instead of quietly
    taking over. ⚠️ BOTH units must set it; the console setting it alone shares
    nothing.
    """
    from webview import webgate
    backend = str(env.get("SESSION_REGISTRY_BACKEND") or "memory").strip().lower()
    if backend == "sqlite":
        return None
    if local:
        webgate.warn_shared_registry_once()
        return None
    return (SHARED_REGISTRY_MESSAGE + f". The backend is {backend!r}, so a "
            "message sent from here to a live session would RESUME it in this "
            "process: two processes stepping one session, two message "
            "histories, one workspace. Set SESSION_REGISTRY_BACKEND=sqlite on "
            "BOTH units (the console alone shares nothing).")
