"""A model with no vision must say so, not guess (owner decision, 2026-09-13);
095 W1a (D5 a): the frozen prompt claims nothing, the image turn carries a route note.

Prod runs glm-5 via zai-coding, declared ``supports_vision=False``: every session
logs "use_vision disabled … has no vision support" and the image content it
receives is replaced with an ``[IMAGE]`` placeholder by
``modules/llm/adapters.py``. Before this block, ``include_vision=False`` rendered
an EMPTY section — the agent was given no instruction at all and could narrate an
image it had never seen.

The owner's call was "honest refusal only": the file IS saved in the workspace,
the agent is told the path, and it states plainly that it cannot see the content.
"""
from agents.task.agent.prompts import SystemPrompt

TELEGRAM = {"surface_id": "telegram", "max_message_bytes": 4096, "media_out": True,
            "markdown_flavor": "html", "supports_interactive_ask": False}


def _prompt(**kwargs) -> str:
    return SystemPrompt("actions", tool_ids=[], **kwargs).get_system_message().content


def test_the_frozen_prompt_claims_no_vision_either_way():
    """095 W1a (D5 a): the prompt outlives the model (a /model switch, a provider
    fallback), so it never says "you can" or "you cannot" see."""
    for include_vision in (True, False):
        content = _prompt(include_vision=include_vision)
        assert "YOU HAVE VISION" not in content and "NO VISION" not in content
        assert "route note" in content


def test_the_prompt_keeps_the_honest_refusal_rule():
    content = _prompt(include_vision=False).lower()
    assert "never guess, describe or infer" in content
    assert "cannot see images" in content
    assert "filesystem_read_file" in content


def test_route_note_follows_the_current_model():
    from core.surfaces.inbound_attachments import IMAGE_PATH_MARKER, image_route_note
    text = f"look\n\n[Attached image: a.png (1 KB)]\n{IMAGE_PATH_MARKER}inbound/a.png"
    seeing = image_route_note(True, "gpt-5", [text])
    assert "image attached natively" in seeing and "gpt-5" in seeing
    blind = image_route_note(False, "glm-5", [text])
    assert "your current model (glm-5) cannot see images" in blind
    assert "saved at inbound/a.png" in blind and "natively" not in blind
    assert "path named above" in image_route_note(False, "", ["no marker"])


def test_image_turn_carries_the_route_note_from_include_vision():
    """The note rides the turn the image is attached to, read from the manager's
    CURRENT ``_include_vision`` (a model change re-resolves it)."""
    from agents.task.agent.message_manager.service import MessageManager
    from core.surfaces.inbound_attachments import IMAGE_PATH_MARKER
    from modules.llm.messages import HumanMessage
    mm = MessageManager(llm=None, task="t", action_descriptions="x", include_vision=True,
                        system_prompt_class=SystemPrompt, max_input_tokens=100000)
    mm._include_vision = False  # a swap to a blind model
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    mm.inject_user_guidance([{"text": f"see\n{IMAGE_PATH_MARKER}inbound/b.png", "kind": "comment",
                           "metadata": {"image_attachments": [img]}}])
    last = [m.message for m in mm.history.messages if isinstance(m.message, HumanMessage)][-1]
    text = last.content[0]["text"]
    assert "cannot see images" in text and "inbound/b.png" in text


def test_the_media_surface_rule_names_the_one_file_delivery_rule(monkeypatch):
    """F6 (2026-09-29): one rule, consistent with core/surfaces/path_links.py —
    an ABSOLUTE workspace path in a message is attached or linked; a relative
    name is not; message(media_paths=...) sends a file on its own."""
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "true")
    content = _prompt(surface=TELEGRAM)
    assert "message(media_paths=" in content
    lowered = content.lower()
    assert "one call per file" in lowered
    assert "absolute workspace path" in lowered
    assert "a relative name is not resolved" in lowered
