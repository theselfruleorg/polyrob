"""Review F8 (2026-09-29): CharacterManager read the persona name from the config
only, never PERSONALITY_DEFAULT_CHARACTER, so a chat seat rendered the packaged
persona while the CLI (persona_resolver) rendered the configured one."""
import asyncio

from agents.personality.character_manager import CharacterManager
from agents.personality.persona_resolver import DEFAULT_CHARACTER_NAME


class _Cfg:
    data_dir = None

    def __init__(self, value=None):
        self._value = value

    def get(self, key, default=None):
        return self._value if (key == 'personality.default_character' and self._value) else default


def _mgr(value=None):
    return CharacterManager("t", _Cfg(value), container=None)


def test_the_env_wins_like_the_persona_resolver(monkeypatch):
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "dangerob")
    assert _mgr("other")._default_character_name() == "dangerob"


def test_the_config_then_the_packaged_default(monkeypatch):
    monkeypatch.delenv("PERSONALITY_DEFAULT_CHARACTER", raising=False)
    assert _mgr("other")._default_character_name() == "other"
    assert _mgr()._default_character_name() == DEFAULT_CHARACTER_NAME


def test_get_default_character_loads_the_env_named_file(monkeypatch, tmp_path):
    (tmp_path / "dangerob.character.json").write_text("{}")
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "dangerob")
    m = _mgr("other")
    m.characters_dir = tmp_path
    seen = []

    async def _load(path):
        seen.append(path)
        return object()
    m._load_character = _load
    assert asyncio.run(m.get_default_character()) is not None
    assert seen and seen[0].name == "dangerob.character.json"
