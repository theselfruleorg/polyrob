"""Owner preference replies for the Telegram admin verbs: /prefs and /config.

Moved out of ``surfaces/telegram/harness.py`` (W9, 2026-10-10 structural
review) unchanged; the harness re-exports every name. Imports nothing from the
harness (no import cycle).
"""
import logging

logger = logging.getLogger("surfaces.telegram.harness")


def _is_configured_source(source: str) -> bool:
    """True when a preference's effective value came from real configuration.

    ``built-in`` / ``default(...)`` mean nobody set it — those rows are the bulk
    of the listing and carry no information the owner asked for.
    """
    s = str(source or "")
    return not (s == "built-in" or s.startswith("default"))


def _prefs_home() -> str:
    """The ONE prefs home (D38).

    ⚠️ Every other admin read here resolves ``config.data_dir`` — correct for
    the goal board, the pending queue and the cron store, and WRONG for
    preferences: ``core.runtime_paths.prefs_home_dir`` is where
    ``write_preference`` and every resolver actually look. Reading a shadow
    tree is why the owner's `/config set delivery.daily_cap` appeared to work
    and changed nothing (2026-09-15). Fail-open to the data home, LOUDLY.
    """
    try:
        from core.runtime_paths import prefs_home_dir
        return prefs_home_dir()
    except Exception as e:  # pragma: no cover - a path resolver fault
        logger.warning("telegram: prefs home unresolved (%s) — falling back", e)
        from core.runtime_paths import effective_data_home
        return effective_data_home()


def _prefs_reply(user_id: str, data_dir: str, instance_id: str,
                 full: bool = False) -> str:
    """owner-UX P4 T2: read-only resolved-preferences summary via the display
    SSOT (`core.prefs.display_effective`) — never the raw file, always the
    effective (pref/env/merged) value + source.

    ⚠️ D38: reads ``prefs_home_dir()``, not the caller's ``data_dir``. The two
    differ on a deployed box, and this is the screen the owner checks to
    confirm a write landed.

    Bare `/prefs` shows only what is actually SET (G15): the full schema is 26
    keys across group headers, which is ~35 lines on a phone in answer to what
    is usually a one-key question. `/prefs all` (and `/config list`) keep the
    complete listing.
    """
    from core.prefs import PREF_SCHEMA, display_effective
    home = _prefs_home()
    rows = []
    for key in sorted(PREF_SCHEMA):
        value, source = display_effective(key, user_id, home, instance_id=instance_id)
        rows.append((key, value, source))
    if not full:
        configured = [r for r in rows if _is_configured_source(r[2])]
        if not configured:
            return ("No preferences set — everything is on its built-in default.\n"
                    "See them all with /prefs all; change one with "
                    "/config set <key> <value>.")
        lines = [f"Set preferences ({len(configured)} of {len(rows)}):"]
        lines += [f"  {key} = {value} ({source})" for key, value, source in configured]
        lines.append("")
        lines.append(f"The other {len(rows) - len(configured)} are on built-in defaults "
                     "— /prefs all shows everything.")
        return "\n".join(lines)
    by_group: dict = {}
    for key, value, source in rows:
        by_group.setdefault(key.split(".", 1)[0], []).append((key, value, source))
    lines = ["Your preferences (read-only):"]
    for group in sorted(by_group):
        lines.append(f"[{group}]")
        for key, value, source in by_group[group]:
            lines.append(f"  {key} = {value} ({source})")
    lines.append("")
    lines.append("tell me what to change — guarded changes arrive as /pending proposals")
    return "\n".join(lines)


def _config_reply(user_id: str, data_dir: str, instance_id: str, args: list) -> str:
    """owner-UX T10: `/config` — the read/write control-plane counterpart to
    the read-only `/prefs`.

    No args (or `list`) renders the SAME PREF_SCHEMA listing `/prefs` does —
    reuses :func:`_prefs_reply`, never a second PREF_SCHEMA loop.

    ⚠️ D37: `set` routes through the ONE write oracle
    (``core.config_service.set_value``, ``surface="telegram"``) instead of
    re-deriving the rules. This seat used to own a private copy of the
    safe-vs-guarded ladder, which meant it knew only PREFERENCES — so every
    flag key answered "unknown preference key", and the oracle's
    credential-surface refusal (an owner binding, a money bound, an approval
    policy or a trust posture is never writable from a REMOTE surface) was
    enforced at the console and not here.

    ⚠️ D38: preferences are read and written under ``prefs_home_dir()``, the
    ONE prefs home. Resolving it from ``config.data_dir`` wrote into a SHADOW
    tree on a deployed box, which is how the owner could not raise his own
    delivery cap from chat (2026-09-15).
    """
    if not args or args[0].lower() == "list":
        # `/config` and `/config list` keep the FULL schema listing — this is the
        # control plane, where seeing every writable key is the point.
        return _prefs_reply(user_id, data_dir, instance_id, full=True)
    sub = args[0].lower()
    if sub != "set":
        return ("Usage: /config [list] | /config set <key> <value>\n"
                "See /prefs for the full read-only listing.")
    rest = args[1:]
    if len(rest) < 2:
        return "Usage: /config set <key> <value>"
    key = rest[0]
    value = " ".join(rest[1:]).strip()
    from core.config_service import set_value
    from core.runtime_paths import prefs_home_dir
    try:
        res = set_value(key, value, user_id=user_id, home_dir=prefs_home_dir(),
                        surface="telegram")
    except Exception as exc:                       # the oracle promises not to
        logger.warning("telegram /config set failed", exc_info=True)
        return (f"I could not apply that ({type(exc).__name__}: "
                f"{str(exc)[:120]}) — nothing was written.")
    if not res.ok:
        from core.prefs import PREF_SCHEMA
        hint = ""
        if res.outcome == "refused" and key not in PREF_SCHEMA:
            groups = sorted({k.split(".", 1)[0] for k in PREF_SCHEMA})
            hint = f"\nPreference groups: {', '.join(groups)}. /config lists them."
        return f"❌ {res.message}{hint}"
    if res.outcome == "queued":
        return (f"'{key}' is guarded — queued for review (not written yet).\n"
                "See /pending, approve with /approve <id> (or /reject <id>).")
    applies = f" (applies: {res.applies})" if res.applies else ""
    # The honesty notes (where the write applies, a shadowing value, a clamp)
    # are the part of the answer the owner cannot see any other way.
    notes = "".join(f"\n{n}" for n in (getattr(res, "notes", None) or ()))
    return f"✅ Set {key} = {value}{applies}.{notes}"
