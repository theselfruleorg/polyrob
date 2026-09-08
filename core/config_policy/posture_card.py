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
