"""Rail templates — inert, typed OFFERS (036 §4.2). Never read by any runtime path.

The harness ships mechanism and offers, never work. A template is read only by
``/rail new <template>`` (every seat) and becomes a rail only when the owner
creates one. Typed slots mean the owner never types a cron expression.

A Python literal rather than a YAML directory on purpose: it ships in the wheel,
the sdist and the deploy tree through the package itself, with no new asset
path for the packaging and deploy ratchets to chase.

⚠️ A template can NEVER carry ``tools`` — :func:`render` refuses one, and the
rail validator refuses it again. A grant is a separate owner act
(``/rail grant``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

TEMPLATES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "daily-digest",
        "title": "Daily digest",
        "description": "Once a day: what happened, what is open, what needs you.",
        "schedule_template": "every day {time}",
        "slots": [
            {"name": "time", "type": "text", "label": "What time (HH:MM)?", "default": "08:00"},
        ],
        "legs": [
            {"title": "Daily digest",
             "body": "Read the goal board, the last day's runs and the open asks. Write a "
                     "short digest for the owner: what finished, what failed and why, what "
                     "is waiting on them. Say 'nothing new' when nothing is."},
        ],
        "max_live": 1,
    },
    {
        "key": "weekly-review",
        "title": "Weekly review",
        "description": "Once a week: review the week's work against the standing objectives.",
        "schedule_template": "every {day} {time}",
        "slots": [
            {"name": "day", "type": "enum", "label": "Which day?", "default": "monday",
             "options": ["monday", "tuesday", "wednesday", "thursday", "friday",
                         "saturday", "sunday"]},
            {"name": "time", "type": "text", "label": "What time (HH:MM)?", "default": "09:00"},
        ],
        "legs": [
            {"title": "Weekly review",
             "body": "Review the last seven days of goals and their outcomes against each "
                     "active objective. Name what moved, what stalled and one concrete "
                     "change for next week. Do not open new goals — report."},
        ],
        "max_live": 1,
    },
    {
        "key": "topic-watch",
        "title": "Topic watch",
        "description": "A recurring digest on a topic you choose, deduped against what was "
                       "already sent.",
        "schedule_template": "every {interval}",
        "slots": [
            {"name": "topic", "type": "text", "label": "What topic?", "default": "AI agents"},
            {"name": "interval", "type": "enum", "label": "How often?", "default": "24h",
             "options": ["12h", "24h", "7d"]},
        ],
        "legs": [
            {"title": "Topic watch: {topic}",
             "body": "Search for genuinely new developments about {topic} since the last "
                     "run. Skip anything already reported in earlier runs. Report the "
                     "three that matter most with a source link each, or say there was "
                     "nothing new."},
        ],
        "max_live": 1,
        "rig": "research",
    },
    {
        "key": "ship-something",
        "title": "Ship something",
        "description": "A recurring build slot: pick the next small improvement and ship it.",
        "schedule_template": "every {interval}",
        "slots": [
            {"name": "project", "type": "text", "label": "Which project?",
             "default": "the current workspace"},
            {"name": "interval", "type": "enum", "label": "How often?", "default": "24h",
             "options": ["12h", "24h", "7d"]},
        ],
        "legs": [
            {"title": "Ship: next improvement to {project}",
             "body": "Pick the smallest useful improvement to {project} that is not already "
                     "in flight, make it, test it, and report what changed and how you "
                     "verified it."},
        ],
        "max_live": 1,
    },
    {
        "key": "custom",
        "title": "{title}",
        "description": "Your own: a title, what to do each time, and how often.",
        "schedule_template": "{schedule}",
        "slots": [
            {"name": "title", "type": "text", "label": "Name it", "default": ""},
            {"name": "body", "type": "text", "label": "What should each run do?",
             "default": ""},
            {"name": "schedule", "type": "text", "label": "How often (e.g. every 24h)?",
             "default": "every 24h"},
        ],
        "legs": [
            {"title": "{title}", "body": "{body}"},
        ],
        "max_live": 1,
    },
)


def template_keys() -> List[str]:
    return [t["key"] for t in TEMPLATES]


def get_template(key: str) -> Optional[Dict[str, Any]]:
    k = str(key or "").strip().lower()
    return next((t for t in TEMPLATES if t["key"] == k), None)


def render(key: str, values: Optional[Mapping[str, str]] = None
           ) -> Tuple[str, str, Dict[str, Any]]:
    """``(title, body, recurrence)`` for template *key* filled with *values*.

    Raises ``ValueError`` for an unknown template, a missing required slot, an
    enum value outside its options, or a ``tools`` key anywhere."""
    t = get_template(key)
    if t is None:
        raise ValueError(f"unknown template {key!r} (templates: {', '.join(template_keys())})")
    if "tools" in t or any("tools" in leg for leg in t["legs"]):
        raise ValueError(f"template {key!r} carries tools — a template never does")
    given = {str(k).lower(): str(v) for k, v in (values or {}).items()}
    if "tools" in given:
        raise ValueError("a template never carries tools — use /rail grant after creating it")
    filled: Dict[str, str] = {}
    for slot in t["slots"]:
        name = slot["name"]
        value = given.get(name, slot.get("default", ""))
        if not str(value).strip():
            raise ValueError(f"template {key!r} needs {name}= ({slot['label']})")
        if slot.get("type") == "enum" and value not in slot["options"]:
            raise ValueError(f"{name} must be one of {', '.join(slot['options'])}")
        filled[name] = str(value).strip()
    fmt = _SafeFormat(filled)
    legs = [{"title": leg["title"].format_map(fmt), "body": leg["body"].format_map(fmt)}
            for leg in t["legs"]]
    rec: Dict[str, Any] = {"schedule": t["schedule_template"].format_map(fmt), "legs": legs,
                           "max_live": t.get("max_live", len(legs))}
    if t.get("rig"):
        rec["rig"] = t["rig"]
    title = t["title"].format_map(fmt)
    if key == "topic-watch":
        title = f"Topic watch: {filled['topic']}"
    return title, t["description"], rec


class _SafeFormat(dict):
    def __missing__(self, k):  # a literal {x} in a body stays literal
        return "{" + k + "}"


__all__ = ["TEMPLATES", "get_template", "render", "template_keys"]
