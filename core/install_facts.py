"""What this install IS — for the agent, not only for the owner.

The ``<environment>`` block told the agent where it lives (instance, data dir,
workspace, capability axes, host binaries) and nothing about HOW it was
installed. So when a browser task refused, the agent could report the refusal
and never say why: it could not see that the ``browser`` extra was absent, nor
name the one command that fixes it.

Two lines, both cheap and both honest:

* the install record (``core.bootstrap_marker``), or an explicit "not recorded";
* the optional capabilities that are ABSENT right now, probed live rather than
  read from the marker's snapshot — an extra installed after bootstrap would
  otherwise be reported missing forever.

Core-tier on purpose: ``agents/`` may not import ``cli/``, and the install
method is already recorded in the marker, so nothing here needs the updater.
"""
from __future__ import annotations

from typing import List, Optional

#: The extras worth naming to the agent: each one is a capability it can be
#: ASKED for and would otherwise refuse without being able to say why.
#: (extra, one-line capability). A pack's SDK extra (twitter, anysite, ...) is NOT
#: listed here: the pack's own declaration is the one source (``_pack_needs``).
_REPORTABLE = (
    ("browser", "browser automation (navigate/click/screenshot)"),
    ("server", "the REST API and the web console"),
    ("docs", "reading PDF and DOCX files"),
    ("memory-vector", "semantic recall (keyword recall still works)"),
    ("crypto", "the agent wallet and on-chain verbs"),
    ("solana", "the Solana rail"),
    ("telegram", "the Telegram surface"),
    ("voice", "voice-note transcription"),
    ("media", "invoice cards and run GIFs"),
)


def missing_extras() -> List[tuple]:
    """``[(extra, capability, remedy), …]`` for what is absent right now."""
    out = []
    try:
        from core.optional_extras import (MODULES_FOR_EXTRA, extra_available,
                                          pip_hint)
    except Exception:
        return out
    for extra, capability in _REPORTABLE:
        # ⚠️ An extra with no known import names probes as AVAILABLE
        # (``all([])`` is True). Reporting that as "installed" would be a
        # confident claim nobody checked, so such an extra is simply not
        # reported either way. `anysite` is deliberately out of
        # _REPORTABLE for exactly this reason (`anysite` is a console script,
        # probed by the tool itself).
        if not MODULES_FOR_EXTRA.get(extra):
            continue
        try:
            if not extra_available(extra):
                out.append((extra, capability, pip_hint(extra)))
        except Exception:
            continue
    reported = {row[0] for row in out}
    for extra, capability, remedy in _pack_needs():
        if extra not in reported:
            reported.add(extra)
            out.append((extra, capability, remedy))
    return out


def _pack_needs() -> List[tuple]:
    """067 (one install): the first-party packs ship inside polyrob; the tools
    whose SDK extra is absent come from THE pack source (``core.packs.sdk`` over
    each installed pack's ``pack.toml``) — the same answer ``polyrob pack list``
    gives. Never imports a pack; never runs discovery (a process that has not
    discovered packs reports none)."""
    try:
        from core.packs import state
        from core.packs.sdk import needs
    except Exception:
        return []
    out = []
    for rec in state.records():
        if rec.manifest is None or rec.status in (state.REFUSED, state.DISABLED):
            continue
        try:
            found = [n for n in needs(rec.manifest) if n.withheld]
        except Exception:
            continue
        for need in found:
            out.append((need.extra or need.tool,
                        f"the {rec.id} pack's {need.tool} tool", need.remedy()))
    return out


def install_lines(data_home: Optional[str] = None) -> List[str]:
    """One or two lines for the ``<environment>`` block."""
    lines: List[str] = []
    try:
        # The version of the code that RUNS, not the one `polyrob setup` saw.
        from core.version import running_version_line
        lines.append(
            f"Running version: {running_version_line()}. What shipped in a release "
            "comes from the CHANGELOG.md deployed with your code — read it with "
            "agent_status(release_notes='latest' or '<version>'); never guess.")
    except Exception:
        pass
    try:
        from core.bootstrap_marker import describe
        lines.append("Install: " + describe(data_home).replace("bootstrap: ", "", 1))
    except Exception:
        lines.append("Install: not recorded.")

    absent = missing_extras()
    if absent:
        named = "; ".join(f"{cap} — absent, needs `{remedy}`"
                          for _extra, cap, remedy in absent)
        lines.append(
            "Optional capabilities NOT installed: " + named + ". "
            "If asked for one of these, say it is not installed and give that "
            "command — never claim the capability and never guess at a result.")
    return lines


__all__ = ["install_lines", "missing_extras"]
