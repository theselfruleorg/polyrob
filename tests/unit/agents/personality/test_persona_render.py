"""S1 — pure persona renderer + gated resolver.

render_persona_block turns a character dict (Character.to_dict()) into a terse
plain-text block. It is PURE (no Character construction, no container) so the
task-agent core never imports the chat stack — it only ever sees a `str`.
"""
import agents.task.constants as constants
from agents.personality.persona_render import render_persona_block, resolve_persona_block


NOVA = {
    "name": "Nova",
    "bio": "I am Nova, a terse and witty automation agent.",
    "adjectives": ["terse", "witty", "precise"],
    "topics": ["automation", "crypto"],
    "style": {"all": ["Be concise.", "No fluff."]},
    "lore": ["Built to get things done."],
}


def test_render_includes_name_and_bio():
    out = render_persona_block(NOVA)
    assert "Nova" in out
    assert "terse and witty" in out


def test_render_is_deterministic():
    assert render_persona_block(NOVA) == render_persona_block(dict(NOVA))


def test_render_empty_character_is_empty_string():
    assert render_persona_block({}) == ""
    assert render_persona_block(None) == ""


def test_resolve_gated_off_returns_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert resolve_persona_block(NOVA) == ""


def test_resolve_gated_on_returns_rendered(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    out = resolve_persona_block(NOVA)
    assert "Nova" in out
    assert out == render_persona_block(NOVA)


def test_resolve_on_but_no_character_is_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    assert resolve_persona_block(None) == ""


# ---------------------------------------------------------------------------
# F5 — the rendered/stored-only split. A field the block drops is a silent no-op
# for the operator who filled it, so the split is pinned and can only change
# deliberately. The voice samples (postExamples/messageExamples) and
# style.writing render; `knowledge` stays stored-only (facts belong to the
# self-context), and a filled stored-only field warns once and is named to the
# owner by stored_only_note.
# ---------------------------------------------------------------------------


def test_rendered_field_allowlist_is_pinned():
    from agents.personality.persona_render import RENDERED_FIELDS, STORED_ONLY_FIELDS

    assert RENDERED_FIELDS == ("name", "adjectives", "bio", "lore", "topics", "style",
                               "postExamples", "messageExamples")
    assert STORED_ONLY_FIELDS == ("knowledge",)
    assert not set(RENDERED_FIELDS) & set(STORED_ONLY_FIELDS)


def test_rendered_style_buckets_are_pinned():
    from agents.personality.persona_render import RENDERED_STYLE_BUCKETS

    assert RENDERED_STYLE_BUCKETS == ("all", "chat", "speaking", "writing")


def test_ignored_populated_fields_names_only_non_empty_ones():
    from agents.personality.persona_render import ignored_populated_fields

    assert ignored_populated_fields(dict(NOVA)) == []
    assert ignored_populated_fields({**NOVA, "knowledge": ["a"]}) == ["knowledge"]
    assert ignored_populated_fields({**NOVA, "knowledge": []}) == []


def test_stored_only_note_names_the_dropped_field():
    from agents.personality.persona_render import stored_only_note

    assert stored_only_note(dict(NOVA)) == ""
    note = stored_only_note({**NOVA, "knowledge": ["a"]})
    assert "knowledge" in note and "NOT rendered" in note


def test_render_warns_once_when_an_authored_field_is_dropped(caplog):
    import logging

    from agents.personality import persona_render

    persona_render._warned_ignored_fields.clear()
    char = {**NOVA, "name": "Dropper", "knowledge": ["a fact"]}
    with caplog.at_level(logging.WARNING, logger=persona_render.__name__):
        first = persona_render.render_persona_block(char)
        persona_render.render_persona_block(char)

    hits = [r for r in caplog.records if "knowledge" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    assert "a fact" not in first


def test_render_includes_style_writing():
    out = render_persona_block({**NOVA, "style": {"writing": ["Short lines only."]}})
    assert "Short lines only." in out


def test_render_includes_a_bounded_voice_sample():
    from agents.personality.persona_render import MAX_VOICE_SAMPLE_CHARS, MAX_VOICE_SAMPLES

    posts = [f"post number {i}" for i in range(10)] + ["x" * 1000]
    out = render_persona_block({**NOVA, "postExamples": posts,
                                "messageExamples": ["a chat line"]})
    assert "Voice samples" in out
    for i in range(MAX_VOICE_SAMPLES):
        assert f"post number {i}" in out
    assert f"post number {MAX_VOICE_SAMPLES}" not in out
    assert "a chat line" in out
    assert all(len(l) <= MAX_VOICE_SAMPLE_CHARS + 2 for l in out.splitlines()
               if l.startswith("- "))


def test_message_examples_conversation_shape_keeps_only_the_characters_turns():
    convo = [[
        {"user": "{{user1}}", "content": {"text": "the other party asks"}},
        {"user": "Nova", "content": {"text": "nova answers dryly"}},
    ]]
    out = render_persona_block({**NOVA, "messageExamples": convo})
    assert "nova answers dryly" in out
    assert "the other party asks" not in out


def test_render_without_samples_has_no_sample_header():
    assert "Voice samples" not in render_persona_block(dict(NOVA))


def test_style_lines_read_as_separate_sentences():
    out = render_persona_block({**NOVA, "style": {"all": ["Answers first", "Short."]}})
    assert "Style: Answers first. Short." in out


def test_lore_lines_read_as_separate_sentences():
    out = render_persona_block({**NOVA, "lore": ["Was first", "Learned fast."]})
    assert "Background: Was first. Learned fast." in out
