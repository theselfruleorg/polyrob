"""The ONE rule for a registered action's name (coding-agent review B1, 2026-09-24).

The Controller registers a tool method under a namespaced key: tool ``coding``,
method ``run_tests`` -> ``coding_run_tests``. A method that already carries the
tool name keeps it (tool ``perplexity``, method ``perplexity_search`` ->
``perplexity_search``, P2-18). That key is what the action ledger, the feed
events and the approval gate see.

Every reader that names a specific action must build its name with this rule.
``VERIFY_BEFORE_DONE`` matched the bare method names (``str_replace``,
``run_tests``) against the namespaced ledger and never fired in production; the
evidence allowlist and the CLI narrator had the same blind spot.

Pure: stdlib only, so every tier can import it (``tools/controller`` registers
with it; ``agents/task/runtime`` matches with it).
"""
from typing import FrozenSet, Optional


def namespaced_action_name(tool: str, method: str) -> str:
    """The key the Controller registers *tool*'s *method* under."""
    if method == tool or method.startswith(f"{tool}_"):
        return method
    return f"{tool}_{method}"


def action_names(tool: str, *methods: str) -> FrozenSet[str]:
    """The registered keys for several methods of one tool."""
    return frozenset(namespaced_action_name(tool, m) for m in methods)


def bare_action_name(tool: Optional[str], name: str) -> str:
    """Strip *tool*'s namespace from a registered key (the inverse, best effort).

    ``bare_action_name("coding", "coding_run_tests") == "run_tests"``; a key
    that does not carry the prefix comes back unchanged.
    """
    if tool and name.startswith(f"{tool}_") and len(name) > len(tool) + 1:
        return name[len(tool) + 1:]
    return name
