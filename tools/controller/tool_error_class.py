"""The `model` and `error_class` of a `tool_invoked` row (D-9, review 2026-10-14).

`tool_invoked` could say WHICH tool ran and whether it failed, but not on which
model, nor how it failed — so the GLM era left no tool-quality baseline, and a
model switch cannot be measured. These two keys make "tool errors per model, by
cause" one GROUP BY on the event log.

`error_class` is one of: ``timeout`` | ``validation`` | ``refused`` |
``not_found`` | ``provider`` | ``other`` (None on success). It is read from the
error TEXT the controller already builds — the one place every failed action
funnels through — so it is a coarse, honest bucket, never a parse of the tool.
"""
from __future__ import annotations

from typing import Any, Optional

ERROR_CLASSES = ("timeout", "validation", "refused", "not_found", "provider", "other")

# First match wins; the order puts the most specific shape first.
_RULES = (
    ("timeout", ("timed out", "timeout", "deadline exceeded")),
    ("validation", ("validation error", "invalid param", "invalid argument", "field required",
                    "missing required", "failed validation", "invalid action", "pydantic",
                    "unexpected keyword", "must be a", "is not a valid")),
    ("refused", ("refused", "denied", "not allowed", "not permitted", "forbidden",
                 "blocked", "requires approval", "approval required", "permission",
                 "not authorized", "unauthorized", "cap exceeded", "over the cap")),
    ("not_found", ("not found", "no such file", "does not exist", "unknown action",
                   "no such", "404")),
    ("provider", ("rate limit", "429", "502", "503", "504", "service unavailable",
                  "connection error", "connection reset", "connecterror", "api error",
                  "upstream", "bad gateway", "temporarily unavailable")),
)


def classify_tool_error(error: Any) -> Optional[str]:
    """The coarse class of a failed action's error text; None when *error* is empty."""
    if not error:
        return None
    text = str(error).lower()
    for name, needles in _RULES:
        if any(n in text for n in needles):
            return name
    return "other"


def invoking_model(controller: Any, execution_context: Any) -> Optional[str]:
    """The model that asked for this action.

    ``execution_context.metadata["model"]`` when the agent step put it there;
    else the model of the orchestrator's agent with this context's ``agent_id``
    (else its only agent). None when nothing can say — never a guess.
    """
    try:
        meta = getattr(execution_context, "metadata", None) or {}
        model = meta.get("model") if isinstance(meta, dict) else None
        if isinstance(model, str) and model:
            return model
        agents = getattr(getattr(controller, "orchestrator", None), "agents", None) or {}
        if not isinstance(agents, dict) or not agents:
            return None
        agent = agents.get(getattr(execution_context, "agent_id", None))
        if agent is None and len(agents) == 1:
            agent = next(iter(agents.values()))
        llm = getattr(agent, "llm", None)
        name = getattr(llm, "model_name", None) or getattr(llm, "model", None)
        return name if isinstance(name, str) and name and name != "unknown" else None
    except Exception:
        return None
