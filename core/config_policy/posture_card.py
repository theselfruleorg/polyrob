"""The effective-posture card (030 WS-E5, finding C3): ONE builder answering
"what is this instance allowed to do right now, and why" across all capability
axes — rendered identically by `polyrob doctor`, REPL `/autonomy`, the webview
/system page, and Telegram `/status`.

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
