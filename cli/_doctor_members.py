"""`doctor --flags`: the posture-member block (D7, 2026-10-10).

The 50 posture members have no flags-catalog row, so ``flags_report`` in
``cli/commands/doctor.py`` appends this block after the catalog rows.
"""
from __future__ import annotations


_MEMBER_GROUP = "Posture bundles (members)"


def member_flag_lines(env: dict, group_needle, search_needle, changed: bool,
                      show_all: bool) -> tuple:
    """D7: the 50 posture members have no catalog row; show each with its value
    and source (the bundle, or its POLYROB_OVERRIDES entry). A member set on
    its own env line is marked IGNORED. Returns (lines, hidden count)."""
    from core.config_policy.members import (MEMBER_FLAGS, member_flag, member_source,
                                            overrides)
    if group_needle is not None and group_needle not in _MEMBER_GROUP.lower():
        return [], 0
    ov = overrides(env)
    out, hidden = [], 0
    for name in sorted(MEMBER_FLAGS):
        if search_needle is not None and search_needle not in name.lower():
            continue
        direct = (env.get(name) or "").strip()
        interesting = name in ov or bool(direct)
        if changed and not interesting:
            continue
        if not show_all and not interesting:
            hidden += 1
            continue
        try:
            value, source = member_flag(name, env), member_source(name, env)
        except Exception as exc:  # noqa: BLE001
            value, source = "unknown", f"probe failed: {type(exc).__name__}"
        line = f"  {name} = {value}  [{source}]"
        if direct:
            line += f"  — direct line {name}={direct} IGNORED (use POLYROB_OVERRIDES)"
        out.append(line)
    if out:
        out.insert(0, f"## {_MEMBER_GROUP}")
    return out, hidden
