"""F-GIF (live-test): ENABLE_GIF_CREATION must actually gate GIF creation.

AgentConfig.generate_gif defaults True and ENABLE_GIF_CREATION (default false) was a
dead flag — so a headless server made per-conversation GIFs no one consumes.
_resolve_generate_gif makes the flag the master switch.
"""
from agents.task.agent.core.construction import _resolve_generate_gif


def test_disabled_flag_forces_off_even_when_config_true():
    assert _resolve_generate_gif(True, False) is False


def test_disabled_flag_forces_off_for_str_path():
    assert _resolve_generate_gif("/tmp/out.gif", False) is False


def test_enabled_flag_honors_bool_config():
    assert _resolve_generate_gif(True, True) is True


def test_enabled_flag_preserves_str_output_path():
    assert _resolve_generate_gif("/tmp/out.gif", True) == "/tmp/out.gif"


def test_enabled_flag_respects_config_false():
    assert _resolve_generate_gif(False, True) is False
