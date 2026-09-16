"""A model with no vision must say so, not guess (owner decision, 2026-09-13).

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


def test_a_vision_model_is_told_it_can_see():
    content = _prompt(include_vision=True)
    assert "YOU HAVE VISION" in content


def test_a_blind_model_is_told_plainly_that_it_cannot_see():
    content = _prompt(include_vision=False)
    assert "NO VISION" in content
    assert "[IMAGE]" in content


def test_a_blind_model_is_told_never_to_guess_image_content():
    content = _prompt(include_vision=False)
    lowered = content.lower()
    assert "never guess" in lowered or "do not guess" in lowered


def test_a_blind_model_is_told_the_file_is_still_in_the_workspace():
    content = _prompt(include_vision=False)
    assert "inbound/" in content
    assert "filesystem" in content.lower()


def test_a_blind_model_is_not_told_it_has_vision():
    content = _prompt(include_vision=False)
    assert "YOU HAVE VISION" not in content


def test_the_media_surface_rule_says_a_path_in_prose_is_not_a_delivery(monkeypatch):
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "true")
    content = _prompt(surface=TELEGRAM)
    assert "message(media_paths=" in content
    lowered = content.lower()
    assert "one call per file" in lowered
    assert "is not a delivery" in lowered
