"""The effective-posture card (030 WS-E5, finding C3): ONE builder answering
"what is this instance allowed to do right now, and why" across all capability
axes — rendered by `polyrob doctor`, `polyrob autonomy status` and `/mode all`.
``build_answers`` (104) reads it for every owner seat: /mode, /status, REPL /autonomy.

Before this, "posture" meant three different vocabularies scattered over six
non-adjacent doctor lines, `/autonomy` omitted the compute axis entirely, and
the agent's own `<environment>` block knew more about its posture than any
human view did.
"""
import os
from typing import List, Optional

from core.config_policy import policy as _p
from core.config_policy.builder_mode import (
    agent_builder_mode,
    effective_builder_mode,
    ship_clamp_reason,
)


def _env_or_default(name: str) -> str:
    return "env" if (os.getenv(name) or "").strip() else "default"


def build_posture_card() -> List[dict]:
    """Rows: {axis, env, effective, source, note} — pure read, never raises
    per-row (a failing axis reads as 'unknown', not a crash)."""
    rows: List[dict] = []

    def _row(axis: str, env: str, fn, note_fn=None) -> None:
        try:
            effective = fn()
        except Exception:
            effective = "unknown"
        note: Optional[str] = None
        if note_fn is not None:
            try:
                note = note_fn()
            except Exception:
                note = None
        rows.append({"axis": axis, "env": env, "effective": effective,
                     "source": _env_or_default(env), "note": note or ""})

    _row("trust profile (local mode)", "POLYROB_LOCAL",
         lambda: "on" if _p.local_mode_enabled() else "off")
    _row("autonomy master", "AUTONOMY_ENABLED",
         lambda: "on" if _p.autonomy_enabled() else "off")

    def _mode_note() -> str:
        raw = (os.getenv("AUTONOMY_MODE") or "").strip().lower()
        if raw == "autonomous" and not _p.full_autonomy_enabled():
            reason = None
            try:
                reason = _p.full_autonomy_clamp_reason()
            except Exception:
                pass
            return f"CLAMPED to supervised — {reason or 'owner not bound / not local'}"
        return ""

    _row("capability mode", "AUTONOMY_MODE",
         lambda: "autonomous" if _p.full_autonomy_enabled() else "supervised",
         _mode_note)
    _row("loop posture", "AUTONOMY_POSTURE", _p.autonomy_posture)

    def _compute_note() -> str:
        raw = (os.getenv("AGENT_COMPUTE_POSTURE") or "").strip()
        try:
            frozen = _p.compute_posture()
        except Exception:
            return ""
        if raw and raw != str(frozen):
            return f"INERT env value {raw!r} — frozen at import as {frozen}; restart applies it"
        return "frozen at import"

    _row("compute posture", "AGENT_COMPUTE_POSTURE", _p.compute_posture,
         _compute_note)

    def _builder_note() -> str:
        """A `ship` instance clamped to `build` for want of a domain or a
        certificate must SAY so. Before 058 WS-3 the clamp was a one-time WARN
        in the log and nothing else — `builder_mode_display()` had existed since
        032 with the docstring "one-line display for status seats" and ZERO
        callers outside its own test, so an operator who set `ship` could be
        running `build` with every seat silent about it.

        ⚠️ Gated on what was REQUESTED. ``ship_clamp_reason()`` answers for any
        deployment without a base domain, so reading it unconditionally made an
        `off` instance report a clamp that never happened — a confident lie
        about a downgrade, which is worse than the silence it replaced."""
        if agent_builder_mode() != "ship":
            return ""
        reason = ship_clamp_reason()
        return f"CLAMPED to build — {reason}" if reason else ""

    _row("builder mode", "AGENT_BUILDER_MODE", effective_builder_mode, _builder_note)

    # The money regime: derived from AUTONOMY_MODE + the DEFI_* arm keys + the
    # daily cap (core/config_policy/money_regime.py). A partial arm names the
    # missing keys here rather than surfacing as a refusal inside a goal run.
    def _money_regime() -> str:
        from core.config_policy.money_regime import money_regime_display
        return money_regime_display()

    _row("money regime", "DEFI_AGENT_AUTONOMY", _money_regime)

    def _pause() -> str:
        try:
            from core.autonomy_control import read_state
            st = read_state()
            if not st.paused:
                return "running"
            return "PAUSED (" + ("everything" if "all" in st.scopes else ", ".join(st.scopes)) + ")"
        except Exception:
            return "unknown (probe failed => treated as paused)"

    rows.append({"axis": "owner pause", "env": "AUTONOMY_PAUSE.json",
                 "effective": _pause(), "source": "record (+ legacy file/env facets)",
                 "note": "live: /pause /resume, polyrob autonomy pause|resume (any seat)"})

    raw_console = (os.getenv("POLYROB_POSTURE") or "").strip()
    if raw_console:
        rows.append({"axis": "console posture", "env": "POLYROB_POSTURE",
                     "effective": raw_console, "source": "env",
                     "note": "webview auth surface"})

    # 058 WS-3: the card's last line, so an operator who has never opened
    # CONFIGURATION.md learns from the card itself that the BUNDLES above are the
    # intended surface and the 700-odd individual knobs are the advanced lane.
    # Imported here, not at module scope: core/config_policy/ must not take an
    # import-time dependency on the generated catalog literal.
    try:
        from core.flags_catalog import CATALOG
        knobs = f"{len(CATALOG)} documented"
    except Exception:
        knobs = "unknown"
    rows.append({"axis": "knobs", "env": "—", "effective": knobs,
                 "source": "catalog",
                 "note": "the axes above set their defaults; "
                         "`polyrob doctor --flags --changed` shows what you moved"})
    return rows


#: What a person calls each seat, for the first answer. Any chat app reads "this chat app".
_SEAT_WORDS = {"terminal": "at the terminal", "console": "in this console",
               "telegram": "in Telegram"}


def asks_first_actions(user_id: Optional[str] = None) -> List[str]:
    """The action names that wait for the owner on THIS instance: the operator's list, the
    recommended set at compute level 2 and up, and the owner's ``approvals.require`` list.

    The same three sources as ``tools.controller.approval.resolve_gated_actions`` plus the
    pref overlay, read from the live env: a console or a shell asks about the agent's next
    start, not about a frozen process. A store fault raises, so the caller can say
    "unavailable" instead of "nothing asks"."""
    raw = os.getenv("APPROVAL_REQUIRED_TOOLS") or ""
    out = {t.strip() for t in raw.split(",") if t.strip()}
    if _p.compute_posture() >= 2:
        from core.verb_policy import ids_where
        out |= ids_where(approval=lambda lanes: "recommended" in lanes or "posture2" in lanes)
    from core.admin_data_home import admin_owner_principal
    from core.prefs import resolve
    from core.runtime_paths import prefs_home_dir
    pref = resolve("approvals.require", user_id or admin_owner_principal(), prefs_home_dir())
    if isinstance(pref, str):
        pref = pref.split(",")
    out |= {str(t).strip() for t in (pref or ()) if str(t).strip()}
    return sorted(out)


def effective_approval_provider(user_id: Optional[str] = None) -> str:
    """What answers a listed action, on the same ladder as
    ``tools.controller.approval.resolve_gated_actions`` (the policy chain's pref tighten
    included). A test pins the two together, because ``core`` may not import ``tools``."""
    provider = (os.getenv("APPROVAL_PROVIDER") or "auto").strip().lower() or "auto"
    if _p.compute_posture() >= 2 and provider == "auto":
        provider = "interactive_cli"
    if _p.full_autonomy_enabled() and provider in ("auto", "interactive_cli"):
        provider = "auto_notify"
    from core.admin_data_home import admin_owner_principal
    from core.prefs import resolve
    from core.runtime_paths import prefs_home_dir
    pref = resolve("approvals.provider", user_id or admin_owner_principal(), prefs_home_dir())
    from core.prefs import _PROVIDER_ORDER as order
    if pref in order and provider in order and order.index(pref) > order.index(provider):
        provider = str(pref)
    return provider


def _pref_asks(user_id: Optional[str]) -> set:
    from core.admin_data_home import admin_owner_principal
    from core.prefs import resolve
    from core.runtime_paths import prefs_home_dir
    pref = resolve("approvals.require", user_id or admin_owner_principal(), prefs_home_dir(),
                   env_value=[], default=[])
    return {str(t).strip() for t in (pref or ()) if str(t).strip()}


def _always_gated() -> set:
    from core.verb_policy import ids_where
    return set(ids_where(approval=lambda lanes: "always_queued" in lanes))


def _file_actions() -> set:
    """The actions a person means by "change files". One that really asks moves the words."""
    from core.verb_policy import ids_where
    return {v for v in ids_where(output=True) if v.startswith(("coding_", "filesystem_"))}


def asks_first_answer(user_id: Optional[str] = None) -> tuple:
    """``(sentence, really_asking)``: what waits for the owner. A listed action under the
    default ``auto`` provider runs with no question (the walk ran it), so a list alone
    must never read as "asks"."""
    asks = asks_first_actions(user_id)
    if not asks:
        return "nothing else is set to ask.", []
    provider = effective_approval_provider(user_id)
    names = ", ".join(asks)
    if provider == "auto":
        return (f"nothing. `{names}` is listed, but it runs with no question until "
                "`approvals.provider` is set."), []
    if provider == "auto_notify":
        # autonomous_gating_lanes: an owner pin and an always-gated verb still wait.
        queued = sorted(set(asks) & (_pref_asks(user_id) | _always_gated()))
        rest = sorted(set(asks) - set(queued))
        told = f" `{', '.join(rest)}` runs, and it tells you afterward." if rest else ""
        if queued:
            return f"{', '.join(queued)}.{told}", queued
        return f"nothing.{told}", []
    if provider == "deny":
        return f"nothing, because `{names}` is refused.", asks
    return f"{names}.", asks


def _money_line(regime: str) -> str:
    """The spend lane decides "without asking", in an owner chat too; the regime decides a
    goal or job. A ceiling that cannot be read fails closed to 0, as the lane itself does."""
    from core.config_policy.spend_lane import autonomous_ceiling_usd, tiered_spend_lane_enabled
    limit = autonomous_ceiling_usd() if tiered_spend_lane_enabled() else 0.0
    if regime.startswith("armed"):
        return (f"Money: goals and jobs may trade alone, up to ${limit:g} a payment and within "
                "your limits; more than that asks you.")
    if limit > 0:
        return f"Money: up to ${limit:g} a payment without asking, within your limits; more asks you."
    return "Money: only when you confirm it."


def _sandbox_two_line(user_id: Optional[str]) -> str:
    """Level 2 gates the shell, and the provider decides what that gate does."""
    try:
        provider = effective_approval_provider(user_id)
    except Exception as e:
        return f"Commands: only in a sandbox; who answers is unavailable({type(e).__name__})."
    tail = {"deny": "and each one that needs asking is refused",
            "auto": "and it does not ask",
            "auto_notify": "and it tells you afterward"}.get(provider, "and it asks first about a risky one")
    return f"Commands: only in a sandbox, {tail}."


def _host_commands_line() -> str:
    """Level 3 alone does not open the host: the same gates as ``host_shell_refusal``."""
    from core.security.host_execution import model_code_host_refusal
    if not _p.local_mode_enabled() or model_code_host_refusal():
        return "Commands: only in a sandbox, because this computer is not set up for host commands."
    return "Commands on this computer: yes, from the terminal; elsewhere only in a sandbox."


def _permission_lines(rows: List[dict], user_id: Optional[str]) -> List[str]:
    by_env = {r["env"]: r for r in rows}
    without = ["read the web"]
    try:
        sentence, really = asks_first_answer(user_id)
        if not _file_actions() & set(really):
            without.append("change files in its work folder")
        asks_line = "Asks first: " + sentence
    except Exception as e:
        asks_line = f"Asks first: unavailable({type(e).__name__})."
    money = _money_line(str((by_env.get("DEFI_AGENT_AUTONOMY") or {}).get("effective") or ""))
    compute = (by_env.get("AGENT_COMPUTE_POSTURE") or {}).get("effective")
    commands = {
        0: "Commands on this computer: no.",
        1: "Commands: only in a sandbox.",
        2: _sandbox_two_line(user_id),
        3: _host_commands_line(),
    }.get(compute, "Commands: unavailable(not readable).")
    return ["Without asking: " + ", ".join(without) + ".", asks_line, money, commands]


def _away_line(rows: List[dict]) -> str:
    by_env = {r["env"]: r for r in rows}
    pause = str((by_env.get("AUTONOMY_PAUSE.json") or {}).get("effective") or "")
    if pause.startswith("PAUSED"):
        return "When you are away: paused, so nothing runs until /resume."
    if pause.startswith("unknown"):
        return "When you are away: treated as paused, because the pause record is not readable."
    # What really runs: the loop setting alone does not start scheduled jobs, and a
    # server without the local profile starts no goals.
    from core.config_policy.autonomy_config import AutonomyConfig
    if (by_env.get("AUTONOMY_ENABLED") or {}).get("effective") != "on":
        if AutonomyConfig.cron_enabled():  # the runtime starts the ticker on this alone
            return ("When you are away: scheduled jobs run, because AUTONOMY_POSTURE is full, "
                    "although AUTONOMY_ENABLED is off.")
        return "When you are away: nothing runs until you write."
    parts = [word for on, word in ((AutonomyConfig.goals_enabled(), "goals"),
                                   (AutonomyConfig.cron_enabled(), "scheduled jobs")) if on]
    if not parts:
        return "When you are away: nothing runs, although the autonomy switch is on."
    if (by_env.get("AUTONOMY_POSTURE") or {}).get("effective") != "silent":
        tells = "and it tells you about results and blockers"
    elif AutonomyConfig.goal_notify_on_done():
        tells = "and it tells you only when a goal ends"
    else:
        tells = "and it does not tell you"
    return (f"When you are away: {' and '.join(parts)} run, {tells}, while the agent's process runs.")


def _change_line(seat: str) -> str:
    if seat == "terminal":
        return ("To change: `polyrob config set <name> <value>` (next start). "
                "/pause and /resume apply at once.")
    if seat == "console":
        return "To change: Agent > Settings, or the terminal. Pause applies at once."
    return ("To change from here: `/config set AUTONOMY_ENABLED true|false` or "
            "`/config set AUTONOMY_POSTURE silent|owner-visible|full` (next start). The rest needs "
            "the computer it runs on. /pause and /resume apply at once.")


def _level_line(user_id: Optional[str]) -> str:
    try:
        from core.admin_data_home import admin_owner_principal
        from core.permission_levels import current_level
        from core.runtime_paths import prefs_home_dir
        level = current_level(user_id or admin_owner_principal(), prefs_home_dir())
    except Exception as e:
        return f"Level: unavailable({type(e).__name__})."
    return f"Level: {level}. `/mode ask-first`, `/mode standard` or `/mode on-my-own` changes it."


def build_answers(seat: str = "", *, user_id: Optional[str] = None,
                  rows: Optional[List[dict]] = None) -> List[str]:
    """The four questions a person has, in plain words (104 §2): who, what without asking,
    what when away, how to change. Read from the card; there is no second source.

    ``seat`` empty (a seat-less status read) leaves out the who and how lines."""
    rows = rows if rows is not None else build_posture_card()
    out: List[str] = []
    if seat:
        where = _SEAT_WORDS.get(seat, "in this chat app")
        out.append(f"You are the owner, {where}. Each place keeps its own chats; "
                   "/thread shows them all in order.")
    out.extend(_permission_lines(rows, user_id))
    out.append(_away_line(rows))
    out.append(_level_line(user_id))
    if seat:
        out.append(_change_line(seat))
    return out


def render_posture_card(rows: Optional[List[dict]] = None,
                        *, prefix: str = "") -> List[str]:
    """One line per axis, identical on every seat:
    ``<axis>: <effective> [<source>]  <note>``"""
    rows = rows if rows is not None else build_posture_card()
    out: List[str] = []
    for r in rows:
        line = f"{prefix}{r['axis']}: {r['effective']} [{r['source']}]"
        if r.get("note"):
            line += f" — {r['note']}"
        out.append(line)
    return out
