"""S1 — pure persona renderer + gated resolver.

render_persona_block turns a character dict (Character.to_dict()) into a terse
plain-text block. It is PURE (no Character construction, no container) so the
task-agent core never imports the chat stack — it only ever sees a `str`.
"""
import agents.task.constants as constants
from agents.personality.persona_render import render_persona_block, resolve_persona_block


ROB = {
    "name": "Rob",
    "bio": "I am Rob, a terse and witty automation agent.",
    "adjectives": ["terse", "witty", "precise"],
    "topics": ["automation", "crypto"],
    "style": {"all": ["Be concise.", "No fluff."]},
    "lore": ["Built to get things done."],
}


def test_render_includes_name_and_bio():
    out = render_persona_block(ROB)
    assert "Rob" in out
    assert "terse and witty" in out


def test_render_is_deterministic():
    assert render_persona_block(ROB) == render_persona_block(dict(ROB))


def test_render_empty_character_is_empty_string():
    assert render_persona_block({}) == ""
    assert render_persona_block(None) == ""


def test_resolve_gated_off_returns_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert resolve_persona_block(ROB) == ""


def test_resolve_gated_on_returns_rendered(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    out = resolve_persona_block(ROB)
    assert "Rob" in out
    assert out == render_persona_block(ROB)


def test_resolve_on_but_no_character_is_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    assert resolve_persona_block(None) == ""


# ---------------------------------------------------------------------------
# F5 — four operator-authored fields are consumed by NOTHING on this path.
# `knowledge`, `messageExamples`, `postExamples` and `style.writing` are parsed,
# stored on Character and populated by the shipped presets, but never reach the
# model through render_persona_block. Silently dropping an authored field is the
# defect; these tests pin the split so it can only change deliberately, and pin
# the one-time warning that makes the drop visible.
# ---------------------------------------------------------------------------


def test_rendered_field_allowlist_is_pinned():
    from agents.personality.persona_render import RENDERED_FIELDS, STORED_ONLY_FIELDS

    assert RENDERED_FIELDS == ("name", "adjectives", "bio", "lore", "topics", "style")
    assert STORED_ONLY_FIELDS == (
        "knowledge", "messageExamples", "postExamples", "style.writing",
    )
    assert not set(RENDERED_FIELDS) & {f.split(".")[0] for f in STORED_ONLY_FIELDS
                                       if "." not in f}


def test_rendered_style_buckets_are_pinned():
    from agents.personality.persona_render import RENDERED_STYLE_BUCKETS

    assert RENDERED_STYLE_BUCKETS == ("all", "chat", "speaking")


def test_ignored_populated_fields_names_only_non_empty_ones():
    from agents.personality.persona_render import ignored_populated_fields

    assert ignored_populated_fields(dict(ROB)) == []
    assert ignored_populated_fields({
        **ROB,
        "knowledge": ["a"],
        "messageExamples": [],
        "style": {"all": ["x"], "writing": ["y"]},
    }) == ["knowledge", "style.writing"]


def test_render_warns_once_when_an_authored_field_is_dropped(caplog):
    import logging

    from agents.personality import persona_render

    persona_render._warned_ignored_fields.clear()
    char = {**ROB, "name": "Dropper", "postExamples": ["a post"]}
    with caplog.at_level(logging.WARNING, logger=persona_render.__name__):
        first = persona_render.render_persona_block(char)
        persona_render.render_persona_block(char)

    hits = [r for r in caplog.records if "postExamples" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    # The warning must never change what the model receives.
    assert first == persona_render.render_persona_block(char)


def test_render_output_is_unchanged_by_ignored_fields():
    from agents.personality.persona_render import render_persona_block

    persona_render_out = render_persona_block(dict(ROB))
    noisy = {**ROB, "knowledge": ["k"], "messageExamples": [["m"]],
             "postExamples": ["p"], "style": {**ROB["style"], "writing": ["w"]}}
    assert render_persona_block(noisy) == persona_render_out
