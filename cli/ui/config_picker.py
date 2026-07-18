"""/config interactive picker (018 P2b) — an ADAPTER, not a new widget.

The renderer stack is frozen ("no new rendering abstractions", 2026-07-14
owner rescope; the 2026-07-18 unfreeze granted at most ONE generalized picker).
This module needs none of that allowance: it feeds the EXISTING /model
ReplPicker (`cli/ui/model_selector.py`) verbatim by presenting settings as
`ModelChoice` rows —

- ``provider``      → the group header rendered above rows (pref group / flag group)
- ``model``         → the setting key (what selection resolves to)
- ``display_name``  → the key again (the row label)
- ``pricing_hint``  → ``= <effective> (<source>)`` + badges: ``⛨`` guarded,
                      ``≈`` advisory (prompt-only), ``↻`` restart-applies

so the frozen widget's fuzzy filter (provider+display+model haystack) searches
key AND group for free. Selection returns ``(group, key)``; the /config
handler prefills the input buffer with a ready-to-send ``/config set KEY …``
(bools arrive pre-toggled, guarded keys carry ``--confirm``), letting the
existing completer/validation pipeline do the rest — no editing forms.
"""
from typing import List, Optional

from modules.llm.available_models import ModelChoice


def build_setting_choices(user_id: Optional[str], home_dir) -> List[ModelChoice]:
    """Every known setting as a picker row (prefs first, then catalog flags)."""
    from core import config_service
    rows: List[ModelChoice] = []
    for info in config_service.search("", user_id=user_id, home_dir=home_dir,
                                      limit=2000):
        badges = ""
        if info.sensitivity == "guarded":
            badges += " ⛨"
        if info.enforcement == "advisory":
            badges += " ≈"
        if info.applies.startswith("restart"):
            badges += " ↻"
        rows.append(ModelChoice(
            provider=info.group,
            model=info.key,
            display_name=info.key,
            is_default=False,
            context_window=0,
            pricing_hint=f"= {info.effective} ({info.source}){badges}",
            supports_vision=False,
            supports_tools=False,
        ))
    return rows


def prefill_for(info) -> str:
    """The input-buffer text seeded after picking *info* — one Enter away for
    bools (pre-toggled), value-position cursor for everything else."""
    base = f"/config set {info.key} "
    if info.sensitivity == "guarded":
        base = f"/config set {info.key} --confirm "
    if info.kind == "bool" and isinstance(info.effective, bool):
        return base + ("off" if info.effective else "on")
    return base
