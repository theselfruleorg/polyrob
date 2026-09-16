"""044 T17: a ROOM's style is the ROOM's, not the owner's.

A public session deliberately carries none of the owner's private contract or
style (they describe his DM, not a room full of other humans), which used to
leave a room with no style at all. These pin that the room's own `chat.*`
overlay reaches the agent build — and that a DM is unchanged.
"""
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.core.construction import AgentConstructionMixin
from core.surfaces.chat_policy import set as set_pol

_OWNER = "rob"


class _Probe(AgentConstructionMixin):
    """The construction mixin with nothing but what these three resolvers read."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.logger = logging.getLogger("room-style-probe")


def _orchestrator(tmp_path, *, public: bool, chat_id: str = "-100"):
    caps = SimpleNamespace(max_message_bytes=4096, media_out=True,
                           markdown_flavor="html", supports_interactive_ask=False)
    chat_type = "supergroup" if public else "dm"
    key = f"agent:main:telegram:{chat_type}:{chat_id}"

    class _Container:
        config = SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            if name == "session_chat_registry":
                return SimpleNamespace(
                    resolve=lambda k: {"surface_id": "telegram", "chat_id": chat_id})
            if name == "surface_registry":
                return SimpleNamespace(get=lambda sid: SimpleNamespace(capabilities=caps))
            return None

    return SimpleNamespace(_chat_session_key=key, container=_Container(),
                           _public_session=public, user_id=_OWNER)


@pytest.fixture(autouse=True)
def _owner_env(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER)
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "polyrob")
    monkeypatch.delenv("GROUP_DEFAULT_MODE", raising=False)
    monkeypatch.delenv("GROUP_REQUIRE_MENTION", raising=False)


def _set(tmp_path, key, value, chat_id="-100"):
    ok, msg = set_pol(tmp_path, _OWNER, "telegram", chat_id, key, value)
    assert ok, msg


def test_a_room_resolves_its_own_policy(tmp_path):
    _set(tmp_path, "chat.mode", "active")
    policy = _Probe(_orchestrator(tmp_path, public=True))._room_policy()
    assert policy is not None and policy.mode == "active"


def test_a_dm_has_no_room_policy(tmp_path):
    _set(tmp_path, "chat.mode", "active")
    assert _Probe(_orchestrator(tmp_path, public=False))._room_policy() is None


def test_room_verbosity_beats_the_owner_pref(tmp_path):
    from core.prefs import write_preference

    ok, err = write_preference(tmp_path, _OWNER, "style.verbosity", "detailed")
    assert ok, err
    _set(tmp_path, "chat.verbosity", "terse")
    assert _Probe(_orchestrator(tmp_path, public=True))._resolve_verbosity() == "terse"


def test_a_room_with_no_verbosity_falls_back_to_the_tenant_pref(tmp_path):
    from core.prefs import write_preference

    ok, err = write_preference(tmp_path, _OWNER, "style.verbosity", "detailed")
    assert ok, err
    assert _Probe(_orchestrator(tmp_path, public=True))._resolve_verbosity() == "detailed"


def test_a_dm_still_reads_the_tenant_pref(tmp_path):
    from core.prefs import write_preference

    ok, err = write_preference(tmp_path, _OWNER, "style.verbosity", "detailed")
    assert ok, err
    _set(tmp_path, "chat.verbosity", "terse")
    assert _Probe(_orchestrator(tmp_path, public=False))._resolve_verbosity() == "detailed"


def test_the_room_style_line_carries_the_rooms_own_values(tmp_path):
    _set(tmp_path, "chat.verbosity", "terse")
    _set(tmp_path, "chat.language", "pt-BR")
    _set(tmp_path, "chat.tone", "friendly and brief")
    line = _Probe(_orchestrator(tmp_path, public=True))._room_style_block()
    assert line.startswith("Style: ")
    assert "verbosity terse" in line and "language pt-BR" in line
    assert "tone friendly and brief" in line


def test_a_room_that_set_nothing_has_no_style_line(tmp_path):
    _set(tmp_path, "chat.mode", "active")   # a policy file that says nothing about style
    assert _Probe(_orchestrator(tmp_path, public=True))._room_style_block() == ""


def test_a_dm_has_no_room_style_line(tmp_path):
    _set(tmp_path, "chat.tone", "friendly")
    assert _Probe(_orchestrator(tmp_path, public=False))._room_style_block() == ""


def test_the_room_name_and_instructions_reach_the_surface_profile(tmp_path):
    from core.surfaces.binding import surface_profile

    _set(tmp_path, "chat.name", "The Public Den")
    _set(tmp_path, "chat.instructions", "Answer in Portuguese.")
    profile = surface_profile(_orchestrator(tmp_path, public=True))
    assert profile["chat_name"] == "The Public Den"
    assert profile["chat_instructions"] == "Answer in Portuguese."


def test_a_dm_profile_never_carries_room_settings(tmp_path):
    from core.surfaces.binding import surface_profile

    _set(tmp_path, "chat.name", "The Public Den")
    _set(tmp_path, "chat.instructions", "Answer in Portuguese.")
    profile = surface_profile(_orchestrator(tmp_path, public=False))
    assert profile["chat_name"] == "-100" and profile["chat_instructions"] == ""
