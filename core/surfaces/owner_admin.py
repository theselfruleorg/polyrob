"""WS-D: owner/access quick-access — one read of the access SSOT.

A single place that reports who the agent serves and the per-surface access posture, so
the `polyrob owner` CLI (and any admin surface) doesn't re-derive it from scattered
flags. Owner-by-email is reported as a hard OFF in v1 (a forgeable From: can never
confer OWNER tier until verified-sender lands).

It also owns the owner-admin PRIMITIVES that more than one seat needs — the
pause/resume API over the ONE 031 pause record (``core.autonomy_control``) plus
its two renderers, and the pending-correspondent listing. Both used to live
inline in ``cli/commands/owner.py``, which made them CLI-only: the owner could
authorise autonomous spend from a phone but could not stop it from a phone
(chat-first review 2026-08-22, G9/G10). Every seat now calls these.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.autonomy_control import (LEGACY_ENTRY_FILENAME, LEGACY_HALT_FILENAME,
                                   LEGACY_STREAM_FILENAME, PAUSE_FILENAME, PauseResult,
                                   PauseState, _env_scopes, legacy_names_for, state_bases,
                                   allows as _allows, pause as _pause,
                                   read_state as _read_state, resume as _resume)

logger = logging.getLogger(__name__)

#: Legacy sentinel names — read-only FACETS of the ONE 031 pause record
#: (``core.autonomy_control``). Nothing writes them any more; a ``touch`` still
#: pauses, and ``resume`` clears them.
HALT_FILENAME = LEGACY_HALT_FILENAME
ENTRY_PAUSE_FILENAME = LEGACY_ENTRY_FILENAME
STREAM_PAUSE_FILENAME = LEGACY_STREAM_FILENAME

_halt_bases = state_bases  # back-compat alias for the seats that import it


def halt_file_path(data_dir: Optional[str]) -> str:
    """The pause record path for *data_dir* (the seat's own data home)."""
    bases = state_bases(data_dir)
    return os.path.join(bases[0] if bases else ".", PAUSE_FILENAME)


entry_pause_file_path = halt_file_path
stream_pause_file_path = halt_file_path


def env_halt_active() -> bool:
    """True when ``AUTONOMY_HALT`` is set in the environment — a pause that
    ``resume`` cannot clear (it needs an env-file edit + restart)."""
    return "all" in _env_scopes()


# ---- THE owner pause API (031) ---------------------------------------------------

def pause_state(data_dir: Optional[str] = None) -> PauseState:
    """The live pause state, fail-closed (see ``core.autonomy_control.read_state``)."""
    return _read_state(data_dir)


def pause_autonomy(data_dir: Optional[str], *, scopes=("all",), duration_minutes=None,
                   reason: str = "", via: str = "cli", set_by: str = "owner") -> PauseResult:
    """THE owner pause. Every seat (telegram, REPL, CLI, webview, the agent action)
    calls this and renders the result with :func:`render_pause_result` — one text."""
    return _pause(data_dir, scopes=tuple(scopes), duration_minutes=duration_minutes,
                  set_by=set_by, via=via, reason=reason)


def resume_autonomy_scopes(data_dir: Optional[str], *, scopes=None, via: str = "cli",
                           set_by: str = "owner") -> PauseResult:
    """Lift the pause (``scopes=None`` = everything) or drop the given scopes."""
    return _resume(data_dir, scopes=tuple(scopes) if scopes else None, set_by=set_by, via=via)


def _scope_words(scopes) -> str:
    return "everything" if "all" in scopes else ", ".join(scopes)


def render_pause_result(res: PauseResult, *, resume_hint: str,
                        status_hint: str = "/status", chat: bool = True) -> str:
    """The one pause confirmation text — the VERIFIED (read-back) state, never a
    checkmark on a write. The SEAT supplies its own verbs: ``resume_hint`` (how
    to lift it there), ``status_hint`` (how to see it there) and ``chat`` (a chat
    seat says the chat itself stays on)."""
    st = res.state
    if not res.effective:
        return ("⚠️ Pause NOT effective — the runtime still reports "
                f"{_scope_words(st.scopes) if st.paused else 'running'}. "
                f"Wrote: {', '.join(res.written) or '(nothing)'}"
                + (f". {res.note}" if res.note else ""))
    since = time.strftime("%H:%M UTC", time.gmtime(st.since)) if st.since else "now"
    head = f"⏸ Paused {_scope_words(st.scopes)} (since {since}"
    head += f", by {st.set_by}" if st.set_by else ""
    head += f" via {st.via}" if st.via else ""
    head += ")."
    lines = [head]
    if st.until:
        mins = max(1, int(math.ceil((st.until - time.time()) / 60)))
        lines.append(f"Auto-resumes in {mins} min.")
    if res.held:
        lines.append(str(res.held))
    if "all" in st.scopes:
        lines.append("In-flight goal/cron runs and background delegations are being cancelled; "
                     f"{status_hint} shows the live actors.")
        lines.append(("Still on: this chat, " if chat else "Still on: ")
                     + "crash/security/credit alerts.")
    lines.append(f"Resume with {resume_hint}. {status_hint} shows this first.")
    return "\n".join(lines)


def render_resume_result(res: PauseResult, *, halt_hint: str) -> str:
    """The one resume confirmation text — the VERIFIED (read-back) state. A
    resume that did not take effect is NEVER prefixed with ▶."""
    st = res.state
    if not res.effective:
        if res.note:
            return f"⚠️ Resume NOT effective — {res.note}."
        still = _scope_words(st.scopes) if st.paused else "running"
        return f"⚠️ Resume NOT effective — still paused: {still}."
    if st.paused:
        return f"▶ Resumed; still paused: {_scope_words(st.scopes)}."
    if not res.removed and not res.written:
        return "Autonomy was not paused."
    return f"▶ Autonomy RESUMED. Pause again with {halt_hint}."


# ---- legacy API (kept for every existing seat; thin over the record) ----------

def halt_autonomy(data_dir: Optional[str], *, reason: str = "owner halt") -> Tuple[bool, List[str]]:
    """Pause everything. Returns ``(effective, paths_written)`` — the VERIFIED state."""
    res = pause_autonomy(data_dir, scopes=("all",), reason=reason)
    return res.effective, res.written


def resume_autonomy(data_dir: Optional[str]) -> Tuple[bool, List[str]]:
    """Lift every pause. Returns ``(still_paused, paths_removed)`` — ``still_paused``
    stays True when a pause is set in the ENVIRONMENT (needs an env-file edit)."""
    res = resume_autonomy_scopes(data_dir)
    return res.state.paused, res.removed


def halt_active() -> bool:
    """Is EVERYTHING paused (the `all` scope)? Read through the ONE predicate."""
    return not _allows("dispatch").allowed


def pause_entries(data_dir: Optional[str], *, reason: str = "owner entry pause") -> Tuple[bool, List[str]]:
    """Pause NEW positions (the ``trading`` scope); exits still run."""
    res = pause_autonomy(data_dir, scopes=("trading",), reason=reason)
    return res.effective, res.written


def resume_entries(data_dir: Optional[str]) -> Tuple[bool, List[str]]:
    res = resume_autonomy_scopes(data_dir, scopes=("trading",))
    return not _allows("trade_entry").allowed, res.removed


def entry_pause_active() -> bool:
    return not _allows("trade_entry").allowed


def pause_stream_seeding(data_dir: Optional[str], *, reason: str = "owner stream pause") -> Tuple[bool, List[str]]:
    """Pause NEW stream-manifest reseeds (the ``streams`` scope); live goals untouched."""
    res = pause_autonomy(data_dir, scopes=("streams",), reason=reason)
    return res.effective, res.written


def resume_stream_seeding(data_dir: Optional[str]) -> Tuple[bool, List[str]]:
    res = resume_autonomy_scopes(data_dir, scopes=("streams",))
    return not _allows("seed_stream").allowed, res.removed


def stream_seeding_paused_active() -> bool:
    return not _allows("seed_stream").allowed


def pending_correspondent_items(registry: Any, tenant: str) -> List[Dict[str, Any]]:
    """Pending correspondent bindings for TENANT as owner-pending items (E5).

    Without this in the pending listing, a third party the agent contacted stays
    permanently unroutable with no owner-visible trace — the silent evaporation the
    approval gate exists to prevent.

    ⚠️ AC1: an unreadable registry RAISES. It used to log and return what it
    had collected — an empty list — so `/pending` and the status snapshot
    said "no pending contacts" over a store nobody could read. Every caller
    names the store unreadable instead (AGENTS: an unreadable store is not an
    empty one).
    """
    items: List[Dict[str, Any]] = []
    for r in registry.list(user_id=tenant):
        if r.get("state") != "pending":
            continue
        items.append({
            "kind": "correspondent",
            "id": f"{r['surface']}:{r['address']}",
            "chars": 0,
            # 030 C7: seat-aware remedy — chat CAN decide this (the old hint
            # pointed a phone owner at the CLI, teaching them chat couldn't).
            "preview": (f"{r['surface']}:{r['address']} -> session "
                        f"{r['session_id']}  (approve: /approve "
                        f"{r['surface']}:{r['address']} — or polyrob owner "
                        f"approve {r['surface']} {r['address']})"),
        })
    return items


def _deployed_owner_principal() -> "str | None":
    """The owner principal the DEPLOYED env file declares, or ``None``.

    C33 (2026-09-21): ``owner_access_summary`` read the process environment and
    nothing else, so ``polyrob owner show`` typed in an SSH shell on the
    production box — where systemd exports the binding and the shell does not —
    reported "(unbound)" over a box that has answered to that owner for months.
    Same defect class, same remedy, as the 031 data home and the 035 P0-3
    instance/tenant axes: adopt what the RUNNING SERVICE reads when the shell
    declares nothing; never guess, and never rewrite an unsafe value.

    Fail-open to ``None``: an unreadable deployment env file is "cannot tell",
    which the caller renders as unbound-with-a-reason, never as a binding.
    """
    from core.admin_data_home import deployed_env_value
    from core.instance import is_safe_tenant_id
    for key in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID"):
        try:
            declared = (deployed_env_value(key) or "").strip()
        except Exception:
            return None
        if declared and is_safe_tenant_id(declared):
            return declared
    return None


def owner_access_summary() -> Dict[str, Any]:
    """Who this instance answers to, and on which surfaces.

    ``owner_source`` names WHERE the binding came from: ``"env"`` (this shell /
    this process), ``"deployed"`` (read from the deployment's own env file
    because the shell declared nothing) or ``"none"``. The flags are always the
    process's own — an owner verb runs with the env it was given, and claiming
    the deployment's flag values for a locally-run check would be a second lie.
    """
    import os

    from core.instance import resolve_owner_principal
    from core.surfaces.catalog import owner_seat_ids
    from core.surfaces.config import SurfaceConfig
    # STRICT: report only an EXPLICITLY-bound owner (None = running on the
    # auto-derived instance-id default), so the summary reflects real config.
    principal = resolve_owner_principal(default_to_instance=False)
    source = "env" if principal else "none"
    if not principal and not any(
            (os.environ.get(k) or "").strip()
            for k in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                      "SURFACE_SUPER_ADMIN_USER_IDS")):
        deployed = _deployed_owner_principal()
        if deployed:
            principal, source = deployed, "deployed"
    return {
        "owner_principal": principal,
        "owner_source": source,
        "correspondent_access_enabled": SurfaceConfig.correspondent_access_enabled(),
        "require_approval": SurfaceConfig.correspondent_require_approval(),
        "max_new_correspondents_per_day": SurfaceConfig.correspondent_max_new_per_day(),
        # Every owner seat in the catalog (064 F1) — this listed three of the
        # seven surfaces before; discord/slack/signal/x were silently absent.
        "surfaces": {sid: SurfaceConfig.surface_enabled(sid)
                     for sid in owner_seat_ids()},
        "owner_by_email": False,  # v1: hard OFF (forgeable From:)
    }
