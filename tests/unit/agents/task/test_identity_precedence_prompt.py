"""T1-08 / T1-13 (2026-07-06 structural review).

T1-08: four identity layers (static <identity> line, persona block, pinned
SELF-CONTEXT, RUNTIME-IDENTITY) with no declared precedence — a persona claim or
recalled memory could override the operator-authored self-context.
T1-13: the owner concept was absent from the default prompt: <security> said
"only the user" while the awareness line says "only the OWNER", and the
owner-awareness line only rendered under CORRESPONDENT_ACCESS_ENABLED (default
OFF) even when an owner principal was bound.
"""
import pytest

from agents.task.agent.prompts import SystemPrompt
from core.instance import owner_awareness_line


def _render(native: bool = True, **kw) -> str:
    sp = SystemPrompt(action_description="- done(text): finish", use_native_tools=native, **kw)
    return sp.get_system_message().content


# ------------------------------------------------------------------- T1-08

@pytest.mark.parametrize("native", [True, False])
def test_identity_declares_self_context_authoritative(native):
    c = _render(native)
    identity = c.split("<identity>")[1].split("</identity>")[0]
    assert "SELF-CONTEXT" in identity
    assert "authoritative" in identity


def test_source_precedence_has_identity_clause():
    c = _render()
    sp = c.split("<source-precedence>")[1].split("</source-precedence>")[0]
    assert "WHO YOU ARE" in sp
    assert "SELF-CONTEXT" in sp
    # runtime identity (model/provider) beats persona/recall claims about the model
    assert "RUNTIME-IDENTITY" in sp


# ------------------------------------------------------------------- T1-13

def test_security_section_unifies_on_owner():
    c = _render()
    assert "<security>" in c  # UNTRUSTED_TOOL_RESULT_WRAP defaults ON
    sec = c.split("<security>")[1].split("</security>")[0]
    assert "Only the user" not in sec
    assert "OWNER" in sec


def test_owner_clause_renders_without_correspondent_frame():
    # The owner clause must be available even when correspondent access is OFF —
    # the frame sentence about <correspondent-message> is what stays gated.
    line = owner_awareness_line(
        {"POLYROB_OWNER_USER_ID": "gleb", "POLYROB_INSTANCE_ID": "rob"},
        include_correspondent_frame=False,
    )
    assert "gleb" in line
    assert "correspondent-message" not in line


def test_owner_clause_empty_without_distinct_owner_and_frame_off():
    assert owner_awareness_line({}, include_correspondent_frame=False) == ""
    assert owner_awareness_line(
        {"POLYROB_OWNER_USER_ID": "rob", "POLYROB_INSTANCE_ID": "rob"},
        include_correspondent_frame=False,
    ) == ""


def test_construction_renders_owner_clause_independent_of_correspondent_flag():
    # Wiring: construction passes include_correspondent_frame=<correspondent flag>
    # instead of skipping the line entirely when the flag is off.
    import inspect
    from agents.task.agent.core import construction

    src = inspect.getsource(construction)
    assert "include_correspondent_frame" in src
