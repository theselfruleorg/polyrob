"""Named permission levels (proposal 104 §4): one choice, a shorthand over settings that exist.

A level writes through ``core.config_service.set_value``, so each seat keeps its own rights
and a guarded change still queues. It never reads or writes a money, compute, builder or
trust flag: ``tests/unit/core/test_permission_levels.py`` pins the key list.
"""
from typing import List, Optional, Tuple

#: name → (ask first?, autonomy on?, what a person reads)
LEVELS = {
    "ask-first": (True, False, "Asks you before the recommended actions. Nothing runs when you are away."),
    "standard": (False, False, "Asks only about money. Nothing runs when you are away."),
    "on-my-own": (False, True, "Asks only about money. Scheduled jobs run, and goals on a computer with the local profile; it tells you."),
}

#: Every key a level may write. A money, compute, builder or trust key must never join it.
WRITES = ("approvals.require", "approvals.provider", "AUTONOMY_ENABLED", "AUTONOMY_POSTURE")


def _recommended() -> List[str]:
    from core.verb_policy import ordered_ids_where
    return list(ordered_ids_where(approval=lambda lanes: "recommended" in lanes))


def _pref_list(user_id: str, home_dir) -> List[str]:
    from core.prefs import load_preferences
    raw = load_preferences(home_dir, user_id).get("approvals.require") or []
    return [str(t).strip() for t in raw if str(t).strip()]


def apply_level(name: str, *, surface: str, user_id: str, home_dir) -> Tuple[bool, List[str], bool]:
    """``(all_ok, one line per write, something_queued)``. A list entry the person added stays,
    unless the recommended set holds it too. An unchanged value is not written, so a chat seat
    does not queue a change that changes nothing. Prefs go first: autonomy never turns on
    because of a level whose approval change failed."""
    from core.config_service import set_value
    from core.prefs import load_preferences
    asks, autonomy, _ = LEVELS[name]
    recommended = _recommended()
    listed = _pref_list(user_id, home_dir)
    own = [t for t in listed if t not in recommended]
    want = own + recommended if asks else own
    provider = "owner_queue" if asks else "auto"
    writes = []
    if set(want) != set(listed):
        writes.append(("approvals.require", ",".join(want)))
    if (load_preferences(home_dir, user_id).get("approvals.provider") or "auto") != provider:
        writes.append(("approvals.provider", provider))
    writes.append(("AUTONOMY_ENABLED", "true" if autonomy else "false"))
    # Scheduled jobs need `full`, and `full` starts them with AUTONOMY_ENABLED off: a level
    # that says nothing runs must take it back.
    writes.append(("AUTONOMY_POSTURE", "full" if autonomy else "silent"))
    confirm = surface in ("local", "console")  # a chat seat queues a guarded change
    ok, queued, lines = True, False, []
    for key, value in writes:
        if not ok:
            lines.append(f"{key}: not changed, because the change before it failed.")
            continue
        res = set_value(key, value, surface=surface, user_id=user_id, home_dir=home_dir,
                        confirm=confirm)
        ok = ok and res.ok
        queued = queued or res.outcome == "queued"
        lines.append(res.message)
    return ok, lines, queued


def current_level(user_id: str, home_dir) -> str:
    """The level the EFFECTIVE settings match, else ``custom``: a wizard preset or compute
    level 2 asks first as surely as this table does. Unreadable raises: the caller says so."""
    from core.config_policy import autonomy_enabled, autonomy_posture
    from core.config_policy.posture_card import asks_first_answer
    _, asking = asks_first_answer(user_id)
    from core.config_policy.autonomy_config import AutonomyConfig
    enabled = autonomy_enabled()
    quiet = not enabled and not AutonomyConfig.cron_enabled()  # `full` alone starts jobs
    if set(_recommended()) <= set(asking) and quiet:
        return "ask-first"
    if not asking:
        if quiet:
            return "standard"
        if enabled and autonomy_posture() == "full":
            return "on-my-own"
    return "custom"


def level_reply(name: str, *, surface: str, user_id: Optional[str] = None, home_dir=None) -> str:
    """The one reply every seat prints for ``/mode <level>``."""
    from core.admin_data_home import admin_owner_principal
    from core.runtime_paths import prefs_home_dir
    uid = user_id or admin_owner_principal()
    home = home_dir or prefs_home_dir()
    ok, lines, queued = apply_level(name, surface=surface, user_id=uid, home_dir=home)
    if not ok:
        head = f"Level {name} is not fully set:"
    elif queued:
        head = f"Level {name} is not set yet: its approval change waits for you in /pending."
    else:
        head = f"Level {name}: {LEVELS[name][2]}"
    return "\n".join([head] + [f"• {line}" for line in lines]
                     + ["Compute level 2 and up keeps its own asks, and money keeps its limits."])
