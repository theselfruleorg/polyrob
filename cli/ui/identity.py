"""The agent's display name for the chat UI — the resolved INSTANCE id.

Chat bubbles (``● rob`` / ``rob:``), the activity line (``rob · working…``), the
separator rule, and the streaming box all show the agent's OWN name. That name is
the INSTANCE id (default ``rob``, or ``POLYROB_INSTANCE_ID``), NOT the framework
name (``polyrob``): an operator who renames the instance should see the new name in
every bubble, matching the `polyrob run` banner — not a hardcoded ``rob``.
"""
from __future__ import annotations


def agent_display_name() -> str:
    """Return the agent's display name (resolved instance id; default ``rob``)."""
    from core.instance import resolve_instance_id
    return resolve_instance_id()
