"""Named toolset distributions for datagen rollouts (design §A2).

A data-diversity technique: each rollout independently
Bernoulli-samples a toolset subset from a named distribution so a corpus
spans tool combinations instead of one fixed loadout. Tool ids reference
POLYROB container tools (``tools/__init__.py::TOOL_DESCRIPTORS``); because
registration is flag/posture-dependent, ``validate_distribution`` lets the
runner warn-and-drop ids unavailable in the running process.
"""
from __future__ import annotations

from typing import Iterable

DISTRIBUTIONS: dict[str, dict] = {
    "default": {
        "description": "Core loadout — files, todos, stateless web.",
        "toolsets": {"filesystem": 100, "task": 100, "web_fetch": 100},
    },
    "web_research": {
        "description": "Research mix — web always, structured data often, "
                       "browser sometimes.",
        "toolsets": {"web_fetch": 100, "anysite": 60, "filesystem": 50,
                     "task": 100, "browser_manager": 30},
    },
    "browser": {
        "description": "Browser-first tasks with occasional file output.",
        "toolsets": {"browser_manager": 97, "filesystem": 30, "task": 100},
    },
    "minimal": {
        "description": "Stateless web only.",
        "toolsets": {"web_fetch": 100},
    },
    "filesystem": {
        "description": "Local file/task work only.",
        "toolsets": {"filesystem": 100, "task": 100},
    },
    "balanced": {
        "description": "Everything at 50% for maximum combination diversity.",
        "toolsets": {"filesystem": 50, "task": 50, "web_fetch": 50,
                     "anysite": 50, "browser_manager": 50},
    },
}


def sample_toolsets(name: str, rng) -> list[str]:
    """Independently sample each tool by its percent; never returns empty
    (falls back to the distribution's highest-percent tool)."""
    dist = DISTRIBUTIONS[name]["toolsets"]
    sampled = [tool for tool, pct in dist.items()
               if rng.random() * 100 < pct]
    if not sampled:
        sampled = [max(dist, key=dist.get)]
    return sampled


def validate_distribution(name: str, known_ids: Iterable[str]) -> list[str]:
    """Return the distribution's tool ids NOT present in *known_ids*."""
    known = set(known_ids)
    return [t for t in DISTRIBUTIONS[name]["toolsets"] if t not in known]
