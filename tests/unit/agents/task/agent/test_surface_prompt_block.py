"""The agent knows which surface it is speaking into (chat-first review, G6).

``SurfaceCapabilities`` has always carried the message cap, the media capability
and the ask capability; none of it reached the model, so the agent wrote the
same reply for a 4096-byte phone chat, a 100 KB email and a terminal.
"""
from types import SimpleNamespace

from agents.task.agent.prompts import SystemPrompt

TELEGRAM = {"surface_id": "telegram", "max_message_bytes": 4096, "media_out": True,
            "markdown_flavor": "html", "supports_interactive_ask": False}


def _prompt(**kwargs) -> str:
    return SystemPrompt("actions", tool_ids=[], **kwargs).get_system_message().content


def test_surface_block_states_the_cap_and_the_shape_rule():
    content = _prompt(surface=TELEGRAM)
    assert "<surface>" in content
    assert "telegram" in content and "4096" in content
    assert "Lead with the outcome" in content
    assert "A filesystem path is not an address" in content


def test_surface_block_reflects_the_media_capability():
    with_media = _prompt(surface=TELEGRAM)
    assert "CAN carry files: attach" in with_media
    without = _prompt(surface={**TELEGRAM, "media_out": False})
    assert "cannot carry files: link" in without


def test_surface_block_names_the_attach_verb_only_when_it_is_callable(monkeypatch):
    """G11: "attach it" is not actionable unless the model knows WHICH verb
    attaches — but advertising a call this session cannot make is worse than
    saying nothing (the T8 transparency rule)."""
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "true")
    assert "message(media_paths=" in _prompt(surface=TELEGRAM)
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "false")
    off = _prompt(surface=TELEGRAM)
    assert "message(media_paths=" not in off
    assert "attach the detail" in off  # the preference still stands


def test_no_surface_means_no_block():
    """Goal/cron/`polyrob run`/raw API bind no chat surface — the prompt must be
    byte-identical to the pre-G6 build there."""
    assert "<surface>" not in _prompt()
    assert "<surface>" not in _prompt(surface=None)
    assert "<surface>" not in _prompt(surface={})


def test_surface_block_is_stable_across_builds():
    """Per-session static: two builds with the same profile produce identical
    bytes, so prompt caching is unaffected."""
    assert _prompt(surface=TELEGRAM) == _prompt(surface=TELEGRAM)


def test_communication_contract_does_not_ask_for_a_wall_of_paths():
    """The old contract said 'report completion WITH the concrete evidence (file
    paths, ids, urls)', which reads as 'enumerate the paths' — and that is what
    the agent did.

    C2 moved the shape half of this rule out of <communication-contract> and into
    <message-shape>, which EVERY session gets rather than only autonomous ones —
    strictly broader, so the assertion now reads the whole prompt. The contract
    keeps the autonomy-specific half and points at the shared rules.
    """
    prompt = SystemPrompt("actions", tool_ids=[], autonomous=True)
    text = prompt.get_system_message().content
    assert "not an address" in text
    assert "attaches it, or links it" in text
    contract = prompt._get_communication_contract_content()
    assert "message-shape rules above apply here too" in contract


# ---------------------------------------------------------------------------
# the binding seam that feeds it
# ---------------------------------------------------------------------------

def test_surface_profile_reads_capabilities_from_the_bound_surface():
    from core.surfaces.binding import surface_profile

    caps = SimpleNamespace(max_message_bytes=4096, media_out=True,
                           markdown_flavor="html", supports_interactive_ask=False)

    class _Container:
        def get_service(self, name):
            if name == "session_chat_registry":
                return SimpleNamespace(resolve=lambda k: {"surface_id": "telegram"})
            if name == "surface_registry":
                return SimpleNamespace(get=lambda sid: SimpleNamespace(capabilities=caps))
            return None

    orch = SimpleNamespace(_chat_session_key="telegram:1", container=_Container())
    assert surface_profile(orch) == {
        "surface_id": "telegram", "max_message_bytes": 4096, "media_out": True,
        "markdown_flavor": "html", "supports_interactive_ask": False,
        # 044 T15: a key with no chat_type segment reads as a DM — the legacy,
        # surface-agnostic case, never as a room.
        "chat_id": "", "chat_type": "dm", "chat_name": "",
        # 044 T17: a DM has no per-room policy, so no standing instructions.
        "chat_instructions": "",
        # 046: and no paid room actions — that note is rendered for ROOMS only.
        "chat_paid_actions": "",
    }


def test_surface_profile_reads_the_chat_type_off_the_routing_key():
    """044 T15: the key's 4th segment IS the chat_type
    (session_chat_registry.build_session_key), so the profile cannot disagree
    with the key the turn was routed on."""
    from core.surfaces.binding import surface_profile

    caps = SimpleNamespace(max_message_bytes=4096, media_out=True,
                           markdown_flavor="html", supports_interactive_ask=False)

    class _Container:
        def get_service(self, name):
            if name == "session_chat_registry":
                return SimpleNamespace(
                    resolve=lambda k: {"surface_id": "telegram", "chat_id": "-100"})
            if name == "surface_registry":
                return SimpleNamespace(get=lambda sid: SimpleNamespace(capabilities=caps))
            return None

    for key, expected in (("agent:main:telegram:supergroup:-100", "supergroup"),
                          ("agent:main:telegram:channel:-100", "channel"),
                          ("agent:main:telegram:dm:-100:u1", "dm")):
        p = surface_profile(SimpleNamespace(_chat_session_key=key,
                                            container=_Container()))
        assert p["chat_type"] == expected
        # 044 T17: a room with no `chat.name` written falls back to its id.
        assert p["chat_id"] == "-100" and p["chat_name"] == "-100"
        assert p["chat_instructions"] == ""


def test_surface_profile_is_none_without_a_binding():
    from core.surfaces.binding import surface_profile
    assert surface_profile(SimpleNamespace()) is None
    assert surface_profile(
        SimpleNamespace(_chat_session_key=None, container=object())) is None
