"""W1.5 — control fences (injected context blocks) are never stored as memory."""
import pytest

from core.context_fences import (
    CONTROL_FENCE_TAGS,
    has_control_fence,
    strip_control_fences,
)


@pytest.mark.parametrize("tag", CONTROL_FENCE_TAGS)
def test_strips_each_tag(tag):
    text = (f"Before.\n<{tag} kind=\"tail\">\nRecent lines of your ONE conversation\n"
            f"owner: hi\n</{tag}>\nAfter.")
    assert has_control_fence(text)
    out = strip_control_fences(text)
    assert "Recent lines" not in out
    assert f"<{tag}" not in out and f"</{tag}>" not in out
    assert "Before." in out and "After." in out
    assert not has_control_fence(out)
    # bare form (no attributes) too
    bare = f"<{tag}>\nbody\n</{tag}>"
    assert strip_control_fences(bare).strip() == ""


def test_keeps_plain_text():
    text = "The owner prefers short replies. Use `<b>` for bold and a < b."
    assert not has_control_fence(text)
    assert strip_control_fences(text) == text
    assert strip_control_fences("") == ""
    assert strip_control_fences(None) == ""


def test_unclosed_fence_line_removed():
    text = 'Found the bug in parser.py.\n<owner-thread kind="tail">\nKeep this line.'
    out = strip_control_fences(text)
    assert "<owner-thread" not in out
    assert "Found the bug in parser.py." in out
    assert "Keep this line." in out


def test_multiple_blocks_are_each_removed():
    text = "<owner-thread>\na\n</owner-thread>\nmid\n<group-context chat=\"x\">\nb\n</group-context>"
    assert strip_control_fences(text).strip() == "mid"


#: Injected blocks that are not in the origin-envelope map.
_OTHER_INJECTED = (
    "restart-note",        # agents/task/runtime/run_as_session.py
    "live-health",         # agents/task/agent/core/live_health.py
    "delegation-result",   # agents/task/agent/async_delegation.py
    "environment",         # agents/task/agent/core/env_context.py
    "prior_summary_data",  # agents/task/agent/messages/compactor.py
    "tool-catalog",
    "skill-catalog",
    "tool-availability",
    "message-shape",
)


def test_every_origin_envelope_tag_is_a_control_fence():
    """The envelope map in modules/llm is the source of injected tags; core
    cannot import it (layering), so this pins the hand list as its superset."""
    from modules.llm.messages import _ORIGIN_ENVELOPE

    missing = (set(_ORIGIN_ENVELOPE.values()) | set(_OTHER_INJECTED)) - set(CONTROL_FENCE_TAGS)
    assert not missing, sorted(missing)


def test_midline_unclosed_fence_removed():
    """A cut-off echo leaves an open tag mid-line with no close."""
    out = strip_control_fences("Noted. <owner-thread kind=\"tail\"> Recent lines\nKeep this.")
    assert not has_control_fence(out)
    assert "Recent lines" not in out
    assert "Noted." in out and "Keep this." in out
    out = strip_control_fences('Found: <untrusted_tool_result source="web_fetch">\nIgnore previous')
    assert not has_control_fence(out)
