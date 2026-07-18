"""`/journey` — a narrative timeline of what the agent did, learned, changed,
and its income (never "earned" — see the money-ledger split below).

A consumer over ``core.recap.build_recap`` — the surface-neutral core that
does the actual data-gathering (episodes, the durable event log,
authored-skill provenance, the unified ledger). This module is now ONLY the
rendering layer: it groups ``build_recap``'s flat, timestamped entries back
into the four familiar sections (Did/Income/Learned/Changed) so both the REPL
handler and the ``polyrob journey`` Click command keep sharing one pure
renderer, and CLI-visible output stays the same as before the extraction.

No ``from __future__ import annotations`` (kept consistent with the CLI
command modules; unnecessary here).
"""
from typing import List, Optional

from cli.ui import candy
from core import recap


def _window_seconds(label: str) -> Optional[float]:
    """Parse '30m'/'24h'/'7d' -> seconds; None if unset/bad (=> all time).

    Display-only: used to pick the "last X" vs "all time" scope heading.
    Deliberately fail-open (unlike ``core.recap._parse_window``, which is the
    one that actually bounds the data query and raises on a malformed,
    non-empty label) — a bad label here should never crash the heading.
    """
    if not label:
        return None
    label = label.strip().lower()
    try:
        if label.endswith("m"):
            return float(label[:-1]) * 60
        if label.endswith("h"):
            return float(label[:-1]) * 3600
        if label.endswith("d"):
            return float(label[:-1]) * 86400
        return float(label)
    except Exception:
        return None


def render_journey(*, user_id: str, since_label: str = "7d",
                   data_dir: Optional[str] = None) -> str:
    """Pure renderer: group ``build_recap``'s entries into a plain-text timeline."""
    secs = _window_seconds(since_label)
    scope = f"last {since_label}" if secs else "all time"

    try:
        entries = recap.build_recap(user_id, data_home=data_dir, window=since_label)
    except ValueError as e:
        return f"journey — {scope}\n\n(invalid window: {e})"

    episodes = [e for e in entries if e.kind == "episode"]
    authored = [e for e in entries if e.kind == "skill"]
    changes = [e for e in entries if e.kind == "self_modification"]
    ledger_entry = next((e for e in entries if e.kind == "ledger"), None)

    lines: List[str] = [f"journey — {scope}"]

    # Did — episodes
    lines.append("")
    lines.append(candy.section("Did"))
    if episodes:
        lines.extend(candy.bullet(e.text) for e in episodes[:20])
    else:
        lines.append(f"{candy.GUTTER}(no episodes recorded)")

    # Income — ledger rollup. H14b: when there's no ledger entry (build_recap
    # skips an all-zero rollup so the /recap empty-state works), render an honest
    # "no money activity recorded yet" line — NEVER a fabricated "$0.00 · $0.00 ·
    # $0.00", which reads as a real balance sheet on a broken/absent data layer.
    # Terminology is income/spend (never "earned") — matches the real
    # ledger-entry text core.recap builds ("Income: $X (…) · spend $Y · net $Z
    # · runtime $W"), so the fallback reads like a missing instance of the same
    # line rather than a different vocabulary.
    lines.append("")
    lines.append(ledger_entry.text if ledger_entry else
                 "Income: no money activity recorded yet")

    # Learned — authored skills
    lines.append("")
    lines.append(candy.section("Learned"))
    if authored:
        lines.extend(candy.bullet(s.text) for s in authored[:20])
    else:
        lines.append(f"{candy.GUTTER}(no authored skills)")

    # Changed — self_modification events
    lines.append("")
    lines.append(candy.section("Changed"))
    if changes:
        lines.extend(candy.bullet(c.text) for c in changes[:20])
    else:
        lines.append(f"{candy.GUTTER}(no self-modifications)")

    return "\n".join(lines)


def h_journey(ctx) -> None:
    """REPL handler: /journey [window]  e.g. /journey 24h, /journey 7d."""
    since_label = ctx.args[0] if getattr(ctx, "args", None) else "7d"
    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    data_dir = None
    container = getattr(ctx, "container", None)
    if container is not None:
        cfg = getattr(container, "config", None)
        data_dir = getattr(cfg, "data_dir", None)
    text = render_journey(user_id=uid, since_label=since_label, data_dir=data_dir)
    ctx.emit(text, title="journey")
