"""S1 (chat consolidation) — AgentConfig.persona_block → <identity> injection.

The persona block is the seam that lets the unified Task agent carry the chat
agent's character/personality (collapse ChatAgent → one core). Gate
TASK_PERSONALITY_BLOCK is OFF by default; persona_block defaults to "" so the
system prompt is BYTE-IDENTICAL to today when no persona is supplied.
"""
from agents.task.agent.prompts import SystemPrompt


def _prompt(**kw):
    return SystemPrompt(
        action_description="x",
        use_native_tools=True,
        model_name="gpt-4",
        provider="openai",
        **kw,
    )


def test_no_persona_is_byte_identical_to_baseline():
    # Off-path guarantee: a SystemPrompt built without persona_block, and one
    # built with persona_block="" , both equal the legacy (no-kwarg) prompt.
    baseline = _prompt().get_system_message().content
    empty = _prompt(persona_block="").get_system_message().content
    none = _prompt(persona_block=None).get_system_message().content
    assert empty == baseline
    assert none == baseline


def test_persona_appears_inside_identity_after_static_line():
    marker = "You are Rob, a terse and witty assistant."
    content = _prompt(persona_block=marker).get_system_message().content
    assert marker in content
    # Must land inside <identity>...</identity>
    identity = content.split("<identity>", 1)[1].split("</identity>", 1)[0]
    assert marker in identity
    # Must come AFTER the static identity sentence (cache-stable prefix preserved).
    static = "You are a research and automation specialist"
    assert identity.index(static) < identity.index(marker)


def test_persona_does_not_disturb_rest_of_prompt():
    marker = "PERSONA-XYZ"
    base = _prompt().get_system_message().content
    withp = _prompt(persona_block=marker).get_system_message().content
    # Everything after </identity> is unchanged.
    assert base.split("</identity>", 1)[1] == withp.split("</identity>", 1)[1]


def test_persona_prompt_is_cache_stable():
    marker = "stable-persona"
    assert (
        _prompt(persona_block=marker).get_system_message().content
        == _prompt(persona_block=marker).get_system_message().content
    )
