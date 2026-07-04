"""Agent templates — pre-built (toolset, autonomy, persona) bundles.

A template is a one-pick shortcut that drives the toolset, autonomy level,
and a short persona / voice hint written into ~/.polyrob/.env.

Design notes:
- Every ``toolset`` value MUST be a key in ``TOOLSETS`` (tool_defaults.py).
- No template enables ``code_execution`` or live trade execution.
- ``trading`` maps to the ``research`` toolset (polymarket/hyperliquid are in
  VALID_TOOL_IDS, but adding a bespoke trading toolset would only add read-only
  ids already covered by ``research``; simpler mapping chosen over scope creep).
- ``seeded_skills`` is populated for research, coding, social, and trading
  templates; blank and general have no seeded skills.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentTemplate:
    """Immutable bundle of defaults for a named agent persona."""
    name: str
    toolset: str
    autonomy: str
    persona: str
    seeded_skills: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, AgentTemplate] = {
    "general": AgentTemplate(
        name="general",
        toolset="default",
        autonomy="standard",
        persona="Helpful, concise assistant. Strong generalist.",
    ),
    "research": AgentTemplate(
        name="research",
        toolset="research",
        autonomy="standard",
        persona="Deep researcher. Thorough, cites sources, stays factual.",
        seeded_skills=["web-research", "market-research-brief", "lead-research"],
    ),
    "coding": AgentTemplate(
        name="coding",
        toolset="coding",
        autonomy="standard",
        persona="Expert software engineer. Writes clean, tested, production-quality code.",
        seeded_skills=["coding-workflow", "skill-authoring"],
    ),
    "social": AgentTemplate(
        name="social",
        toolset="social",
        autonomy="standard",
        persona="Social-media and content strategist. Engaging, on-brand voice.",
        seeded_skills=["web-scraping", "lead-research"],
    ),
    # trading → research toolset (reads-only; no code_execution/trade execution).
    "trading": AgentTemplate(
        name="trading",
        toolset="research",
        autonomy="standard",
        persona="Markets researcher. Analyses price action and on-chain data. Never executes trades.",
        seeded_skills=["market-research-brief"],
    ),
    "blank": AgentTemplate(
        name="blank",
        toolset="minimal",
        autonomy="off",
        persona="",
    ),
}


def resolve_template(name) -> AgentTemplate:
    """Return the template for *name*, or ``general`` for unknown names.

    Never raises; accepts any input type.
    """
    try:
        key = str(name or "").strip().lower()
    except Exception:
        key = ""
    return TEMPLATES.get(key, TEMPLATES["general"])


def resolve_template_persona(name: str | None) -> str:
    """Persona text for *name* (``""`` for blank/empty)."""
    return resolve_template(name).persona


def seeded_skills_for(name: str | None) -> list[str]:
    """Seeded skill ids for *name* (empty list if none)."""
    return list(resolve_template(name).seeded_skills)
