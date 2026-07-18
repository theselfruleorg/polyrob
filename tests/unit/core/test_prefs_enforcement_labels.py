"""P0.4 (proposal 018): every pref key declares enforced vs advisory, honestly.

After P0.2/P0.3 every PREF_SCHEMA key except ``style.*`` has a real runtime
consumer. ``style.*`` steer the prompt only (their design) — the panel must say
so instead of letting an owner set them and wonder why nothing hard-changes.
"""
from core.prefs import ENFORCEMENT_ADVISORY, ENFORCEMENT_ENFORCED, PREF_SCHEMA


def test_every_key_declares_enforcement():
    for key, spec in PREF_SCHEMA.items():
        assert spec.enforcement in (ENFORCEMENT_ENFORCED, ENFORCEMENT_ADVISORY), key


def test_style_keys_are_advisory_everything_else_enforced():
    for key, spec in PREF_SCHEMA.items():
        if key.startswith("style."):
            assert spec.enforcement == ENFORCEMENT_ADVISORY, key
        else:
            assert spec.enforcement == ENFORCEMENT_ENFORCED, key


def _ctx(tmp_path):
    from cli.ui.commands.h_config import ConfigCtx
    return ConfigCtx(user_id="u1", home_dir=str(tmp_path))


def test_config_list_marks_advisory_rows(tmp_path):
    from cli.ui.commands.h_config import cmd_config

    out = cmd_config(_ctx(tmp_path), ["list", "style"])
    assert "advisory" in out
    # Enforced groups carry no advisory noise.
    out2 = cmd_config(_ctx(tmp_path), ["list", "goals"])
    assert "advisory" not in out2


def test_config_get_explains_advisory(tmp_path):
    from cli.ui.commands.h_config import cmd_config

    out = cmd_config(_ctx(tmp_path), ["get", "style.tone"])
    assert "advisory" in out
    out2 = cmd_config(_ctx(tmp_path), ["get", "goals.daily_quota"])
    assert "advisory" not in out2
