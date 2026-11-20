"""Owner preference replies: ``/prefs`` and ``/config`` on every chat seat.

Moved below the seats (proposal 125 S14) from ``surfaces/telegram/prefs_ops.py``
(which re-exports the old private names): Telegram, the console chat box and the
REPL render the SAME text. Reads go through the display SSOT
(``core.prefs.display_effective``); ``/config set`` goes through the ONE write
oracle (``core.config_service.set_value``).
"""
import logging

logger = logging.getLogger(__name__)


def is_configured_source(source: str) -> bool:
    """True when a preference's effective value came from real configuration.

    ``built-in`` / ``default(...)`` mean nobody set it — those rows are the bulk
    of the listing and carry no information the owner asked for.
    """
    s = str(source or "")
    return not (s == "built-in" or s.startswith("default"))


def prefs_home() -> str:
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
        logger.warning("prefs home unresolved (%s) — falling back", e)
        from core.runtime_paths import effective_data_home
        return effective_data_home()


def prefs_reply(user_id: str, data_dir: str, instance_id: str,
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
    home = prefs_home()
    rows = []
    for key in sorted(PREF_SCHEMA):
        value, source = display_effective(key, user_id, home, instance_id=instance_id)
        rows.append((key, value, source))
    if not full:
        configured = [r for r in rows if is_configured_source(r[2])]
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


def config_reply(user_id: str, data_dir: str, instance_id: str, args: list, *,
                 surface: str = "telegram") -> str:
    """owner-UX T10: `/config` — the read/write control-plane counterpart to
    the read-only `/prefs`.

    No args (or `list`) renders the SAME PREF_SCHEMA listing `/prefs` does —
    reuses :func:`prefs_reply`, never a second PREF_SCHEMA loop.

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
    # 113 W4 item 4 (D-3): the ONE grammar the terminal and `polyrob config` parse.
    from core.config_service import parse_config_command, unset_value
    cmd = parse_config_command(args)
    if cmd.error:
        return cmd.error
    if cmd.verb == "list":
        # `/config` and `/config list` keep the FULL schema listing — this is the
        # control plane, where seeing every writable key is the point.
        return prefs_reply(user_id, data_dir, instance_id, full=True)
    if cmd.verb in ("get", "explain"):
        return config_read(user_id, cmd.verb, [cmd.key])
    if cmd.verb in ("search", "check"):
        # 120 W6: the ONE chat text (core.config_service), which the agent prints too.
        from core import config_service
        from core.runtime_paths import prefs_home_dir
        if cmd.verb == "check":
            return config_service.check_reply(user_id=user_id, home_dir=prefs_home_dir())
        return config_service.search_reply(cmd.query, user_id=user_id,
                                           home_dir=prefs_home_dir())
    if cmd.verb == "unset":
        return f"❌ {unset_value(cmd.key, surface=surface).message}"
    key, value = cmd.key, cmd.value
    from core.config_service import set_value
    from core.runtime_paths import prefs_home_dir
    try:
        res = set_value(key, value, user_id=user_id, home_dir=prefs_home_dir(),
                        surface=surface)
    except Exception as exc:                       # the oracle promises not to
        logger.warning("/config set failed", exc_info=True)
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


def config_read(user_id: str, sub: str, rest: list) -> str:
    """`/config get <key>` and `/config explain <key>` (080 D8) — the value and
    where it comes from, through the ONE describer (``core.config_service``)
    the REPL and ``polyrob config`` read. ``explain`` adds every rung of the
    provenance chain, highest first. Secrets arrive masked."""
    if not rest:
        return f"Usage: /config {sub} <key>"
    key = rest[0]
    from core import config_service
    from core.runtime_paths import prefs_home_dir
    try:
        fn = config_service.explain if sub == "explain" else config_service.describe
        info = fn(key, user_id=user_id, home_dir=prefs_home_dir())
    except KeyError:
        return config_service.unknown_key_message(key)
    except Exception as exc:  # noqa: BLE001 — an unread setting is not a value
        return f"I could not read {key} ({type(exc).__name__}: {str(exc)[:120]})."
    lines = [f"{key} = {info.effective} ({info.source})"
             + (f" · {info.gate_effective}" if info.gate_effective else ""),
             f"applies: {info.applies}"]
    if info.enforcement == "advisory":
        lines.append("advisory: it steers my prompt only; nothing enforces it")
    if info.description:
        lines.append(info.description)
    if sub == "explain" and info.chain:
        lines.append("Where it comes from (highest wins):")
        lines += [f"  {s.origin}: {s.value}" for s in info.chain]
    return "\n".join(lines)
