"""Untrusted framing at the STATE-MESSAGE render site (S5, 2026-09-14).

UP-06 frames untrusted tool output in `<untrusted_tool_result>` delimiters at the
result→`ToolMessage` choke point (`result_processing.py`), and deliberately wraps
the *string* only — the `ActionResult` itself is never rewritten, so memory
previews and telemetry stay clean. But the very same bytes are rendered a SECOND
time, raw: `step.py` passes `self._last_result` to `add_state_message`, and
`prompts.py::AgentMessagePrompt` prints `Action result N/M: {extracted_content}`
into a plain `HumanMessage`. The live browser DOM text was never framed at all.
So an indirect prompt injection in a fetched page reached the model as
instructions on the next step, with the framing intact one message away.

This module closes that without inventing a second taxonomy:

  ``stamp_result_source`` — called where `result_processing` ALREADY resolved
  `(action_name, tool)` from the controller, records that source on the result's
  ``metadata`` (a label, never the content) when — and only when — the source is
  untrusted by `core.security.untrusted_wrap.is_untrusted_tool`.

  ``maybe_wrap_result_content`` / ``maybe_wrap_browser_dom`` — the render site
  frames from that stamp through the SAME `maybe_wrap` helper, so the skip rules
  (non-str, `< UNTRUSTED_WRAP_MIN_CHARS`, delimiter defanging) cannot drift.

Gated on `UNTRUSTED_TOOL_RESULT_WRAP` exactly like the ToolMessage path: OFF ⇒
nothing is stamped upstream and nothing is wrapped here (byte-identical). An
unstamped result — the legacy non-native path — renders raw rather than being
guessed at.
"""
from __future__ import annotations

from typing import Any, Optional

# metadata key holding the resolved untrusted source; a LABEL, not content.
SOURCE_KEY = "untrusted_source"


def wrap_enabled() -> bool:
    """The UP-06 flag, read live so a test/monkeypatch is honoured. Fail-closed
    to OFF (the legacy shape) if constants cannot be imported."""
    try:
        from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
        return bool(UNTRUSTED_TOOL_RESULT_WRAP)
    except Exception:  # pragma: no cover
        return False


def stamp_result_source(result: Any, action_name: Optional[str],
                        tool: Optional[str]) -> None:
    """Record an UNTRUSTED result's `(action, tool)` on its metadata. Never raises.

    No-op for a trusted source, so an ordinary result's metadata is untouched.
    """
    try:
        from core.security.untrusted_wrap import is_untrusted_tool
        if not is_untrusted_tool(action_name, tool):
            return
        metadata = dict(getattr(result, "metadata", None) or {})
        metadata[SOURCE_KEY] = {"action": action_name, "tool": tool}
        result.metadata = metadata
    except Exception:  # pragma: no cover — framing is best-effort, never fatal
        pass


def maybe_wrap_result_content(result: Any, content: Any) -> Any:
    """Frame *content* when *result* carries an untrusted-source stamp.

    ``content`` is the ALREADY-RENDERED (truncated) string, so the closing
    delimiter can never be cut off by a later truncation.
    """
    if not wrap_enabled():
        return content
    try:
        source = (getattr(result, "metadata", None) or {}).get(SOURCE_KEY)
        if not isinstance(source, dict):
            return content
        from core.security.untrusted_wrap import maybe_wrap
        return maybe_wrap(source.get("action"), source.get("tool"), content)
    except Exception:  # pragma: no cover
        return content


def maybe_wrap_browser_dom(text: Any) -> Any:
    """Frame the live page's interactive-element text as ``source="browser"``.

    The DOM block is attacker-authorable by definition (it IS the page), and it
    is the one untrusted surface that never passed through a tool result at all.
    """
    if not wrap_enabled():
        return text
    try:
        from core.security.untrusted_wrap import maybe_wrap
        # action_name=None ⇒ the frame is labelled with the tool namespace.
        return maybe_wrap(None, "browser", text)
    except Exception:  # pragma: no cover
        return text
