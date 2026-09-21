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


def env_entry_pause_active() -> bool:
    """True when ``TREASURY_ENTRY_PAUSE`` is set in the environment."""
    return "trading" in _env_scopes()


def env_stream_pause_active() -> bool:
    """True when ``STREAM_SEEDING_PAUSE`` is set in the environment."""
    return "streams" in _env_scopes()


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


def apply_owner_intent(intent, data_dir: Optional[str], *, via: str, resume_hint: str,
                       halt_hint: str, force_full_reason: Optional[str] = None
                       ) -> Tuple[Optional[str], Optional[PauseResult]]:
    """The ONE decision path behind the prose stop gates (Telegram text/voice,
    the REPL): ``(reply, result)`` or ``(None, None)`` when the message should
    go to the agent unchanged.

    * ``full_stop`` → pause everything (with the parsed duration).
    * ``resume``    → lift the pause when one is on; otherwise "continue" is chat.
    * ``scoped``    → the agent narrows it — unless *force_full_reason* is given
      (model dead / session busy), in which case everything is paused as the safe
      default and the reason leads the reply.
    """
    if intent is None:
        return None, None
    if intent.kind == "scoped":
        if not force_full_reason:
            return None, None
        res = pause_autonomy(data_dir, scopes=("all",), reason=intent.raw, via=via)
        return (f"⚠️ {force_full_reason}, so I paused everything as the safe default.\n"
                + render_pause_result(res, resume_hint=resume_hint)), res
    if intent.kind == "resume":
        if not pause_state(data_dir).paused:
            return None, None
        res = resume_autonomy_scopes(data_dir, via=via)
        return render_resume_result(res, halt_hint=halt_hint), res
    res = pause_autonomy(data_dir, scopes=("all",), duration_minutes=intent.duration_minutes,
                         reason=intent.raw, via=via)
    return render_pause_result(res, resume_hint=resume_hint), res


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


def owner_pause_phrases(user_id: Optional[str], home_dir: Optional[str]) -> Tuple[str, ...]:
    """The owner's extra object-free stop words (``pause.phrases`` pref, 031) for
    :func:`core.surfaces.owner_intent.owner_stop_intent`. Fail-open to none — the
    gate must work when the pref store does not."""
    try:
        from core import prefs
        out = prefs.resolve("pause.phrases", user_id, home_dir, default=[]) or []
        words: List[str] = []
        for entry in out:  # "dev loop" -> "dev", "loop": the parser matches per word
            words.extend(w for w in str(entry).lower().split() if w)
        return tuple(dict.fromkeys(words))
    except Exception:
        logger.warning("pause.phrases pref read failed — using the built-in set only",
                       exc_info=True)
        return ()


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
    """
    items: List[Dict[str, Any]] = []
    try:
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
    except Exception:
        # L10 (2026-07-15): was a silent `except: pass` — a broken correspondent
        # registry would hide pending contacts with no trace. Log it (the pending
        # listing still degrades gracefully to whatever was collected).
        logger.warning("owner pending: correspondent registry read failed", exc_info=True)
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
        "surfaces": {
            "telegram": SurfaceConfig.telegram_surface_enabled(),
            "whatsapp": SurfaceConfig.whatsapp_surface_enabled(),
            "email": SurfaceConfig.email_surface_enabled(),
        },
        "owner_by_email": False,  # v1: hard OFF (forgeable From:)
    }


# --- the plain-word pending decision (2026-09-15) -----------------------------
#
# The proactive pending notice told the owner, in its own last line, to
# `Reply "approve" / "reject"`. Nothing parsed that word. It fell through to the
# agent as ordinary chat, and the agent has no promote verb of any kind — the
# whole point of the quarantine is that only the owner can lift it. So the
# instruction on the owner's screen did nothing at all.
#
# Two ways to make it honest: delete the promise, or keep it. We keep it. A word
# is the cheapest owner action there is, and the owner reads this on a phone.
#
# The matcher is deliberately narrow. It reads the WHOLE message, not a
# substring, so "I approve of that plan" or "reject the third candidate token"
# is never a decision — it is a sentence for the agent. The caller ALSO requires
# a non-empty queue, so the word only decides when there is something to decide.

#: Words that carry the decision.
#: ⚠️ "ok" and "yes" are deliberately ABSENT. They are the commonest
#: conversational filler there is, and a decision word has to be one the owner
#: could only have meant as a decision — approving a self-modification by
#: accident is not a recoverable mistake.
_APPROVE_WORDS = frozenset({"approve", "approved", "accept", "accepted"})
_REJECT_WORDS = frozenset({"reject", "rejected", "decline", "declined", "discard"})
#: Words that may surround the decision without changing it.
_DECISION_FILLER = frozenset({"yes", "no", "please", "pls", "them", "it", "these",
                              "those", "that", "this", "do", "just", "go", "ahead",
                              "the", "proposals", "proposal", "changes", "change"})
#: The whole-queue word.
_DECISION_ALL = frozenset({"all", "everything", "every"})

#: Beyond this many words it is a sentence, not a decision.
_DECISION_MAX_WORDS = 5


def parse_pending_decision(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """``("/approve", "all")``, ``("/reject", None)`` … or ``None``.

    ``None`` means "this is not a decision" — the message belongs to the agent.
    The second element is ``"all"`` when the owner named the whole queue, and
    ``None`` when they did not (the caller then decides the single waiting item,
    or asks which one).
    """
    import re as _re

    words = [w for w in _re.split(r"[^a-z]+", (text or "").strip().lower()) if w]
    if not words or len(words) > _DECISION_MAX_WORDS:
        return None
    approve = bool(_APPROVE_WORDS & set(words))
    reject = bool(_REJECT_WORDS & set(words))
    if approve == reject:          # neither, or both — not a decision
        return None
    unknown = set(words) - _APPROVE_WORDS - _REJECT_WORDS - _DECISION_FILLER - _DECISION_ALL
    if unknown:
        return None
    target = "all" if (_DECISION_ALL & set(words)) else None
    return ("/approve" if approve else "/reject"), target
