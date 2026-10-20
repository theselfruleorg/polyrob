"""080 D8 — Telegram parity: /model, /skills, /config get|explain, and ONE
complete verb table (the REPL-only verbs are rows with ``seats=("repl",)``)."""
from types import SimpleNamespace

import pytest

from core.config_service import SetResult


# --- the one verb table ----------------------------------------------------- #

def test_every_repl_verb_is_a_row_of_the_one_table():
    from cli.ui.commands import build_default_registry
    from core.verbs import PALETTE_EXCLUDED, verb_for
    missing = sorted("/" + c.name for c in build_default_registry().commands()
                     if verb_for("/" + c.name) is None
                     and "/" + c.name not in PALETTE_EXCLUDED)
    assert not missing, f"REPL verbs outside core/verbs.py: {missing}"


def test_model_and_skills_route_on_every_seat_and_show_in_telegram_help():
    from core.surfaces.dispatcher import _COMMANDS
    from core.verbs import seat_verbs
    from surfaces.telegram import admin_verbs
    from surfaces.telegram.harness import _HELP_BODY, _OWNER_ADMIN_COMMANDS
    for name in ("/model", "/skills"):
        assert name in _COMMANDS and name in _OWNER_ADMIN_COMMANDS
        assert admin_verbs.handler_for(name) is not None
        assert name in {v.name for v in seat_verbs("telegram")}
        assert f"\n{name}" in "\n" + _HELP_BODY


def test_repl_only_rows_stay_out_of_telegram():
    from core.verbs import seat_verbs, verb_for
    tg = {v.name for v in seat_verbs("telegram")}
    for name in ("/doctor", "/exit", "/verbose", "/autonomy"):
        assert verb_for(name).seats == ("repl",) and name not in tg


def test_money_comment_pins_no_count():
    import inspect

    import core.verbs as verbs
    assert "13 money verbs" not in inspect.getsource(verbs)


# --- /model ----------------------------------------------------------------- #

def _info(value, source):
    return SimpleNamespace(effective=value, source=source)


def test_model_reads_the_effective_model_and_its_source(monkeypatch):
    from core import config_service
    from surfaces.telegram import prefs_ops
    monkeypatch.setattr(prefs_ops, "_effective_model",
                        lambda: ("openrouter", "deepseek/deepseek-chat"))
    pins = {"DEFAULT_PROVIDER": _info("openrouter", "env-file:/etc/polyrob/polyrob.env"),
            "DEFAULT_MODEL": _info("deepseek/deepseek-chat", "env-file:/etc/polyrob/polyrob.env")}
    monkeypatch.setattr(config_service, "describe",
                        lambda key, **kw: pins.get(key, _info(None, "default")))
    out = prefs_ops.model_reply("u1", [])
    assert "Model: openrouter/deepseek/deepseek-chat" in out
    assert "DEFAULT_MODEL = deepseek/deepseek-chat (env-file:/etc/polyrob/polyrob.env)" in out
    assert "CHAT_MODEL" not in out


def test_model_with_no_pin_says_so(monkeypatch):
    from core import config_service
    from surfaces.telegram import prefs_ops
    monkeypatch.setattr(prefs_ops, "_effective_model", lambda: ("openai", "gpt-5"))
    monkeypatch.setattr(config_service, "describe", lambda key, **kw: _info(None, "default"))
    assert "Nothing pins it" in prefs_ops.model_reply("u1", [])


def test_model_write_goes_through_the_oracle_and_prints_its_message(monkeypatch):
    from core import config_service
    from surfaces.telegram import prefs_ops
    calls = []

    def fake_set(key, value, **kw):
        calls.append((key, value, kw.get("surface")))
        return SetResult(ok=True, outcome="written", message=f"set {key}")

    monkeypatch.setattr(config_service, "set_value", fake_set)
    out = prefs_ops.model_reply("u1", ["openrouter/deepseek/deepseek-chat"])
    assert calls == [("DEFAULT_PROVIDER", "openrouter", "telegram"),
                     ("DEFAULT_MODEL", "deepseek/deepseek-chat", "telegram")]
    assert "set DEFAULT_PROVIDER" in out and "set DEFAULT_MODEL" in out


def test_model_write_stops_at_the_first_refusal(monkeypatch):
    from core import config_service
    from surfaces.telegram import prefs_ops
    calls = []

    def refuse(key, value, **kw):
        calls.append(key)
        return SetResult(ok=False, outcome="refused", message="CHAT_PROVIDER shadows it")

    monkeypatch.setattr(config_service, "set_value", refuse)
    out = prefs_ops.model_reply("u1", ["openai", "gpt-5"])
    assert calls == ["DEFAULT_PROVIDER"] and "❌ CHAT_PROVIDER shadows it" in out


def test_model_refuses_an_unknown_provider(monkeypatch):
    from core import config_service
    from surfaces.telegram import prefs_ops
    monkeypatch.setattr(config_service, "set_value",
                        lambda *a, **k: pytest.fail("must not write"))
    assert "Unknown provider" in prefs_ops.model_reply("u1", ["nosuchprov/x"])


# --- /skills ---------------------------------------------------------------- #

def test_skills_lists_the_catalog(monkeypatch):
    import agents.task.agent.skill_manager as sm
    from surfaces.telegram.skills_ops import skills_reply
    skills = [SimpleNamespace(skill_id="x-post", description="Post on X"),
              SimpleNamespace(skill_id="digest", description="The daily digest")]
    monkeypatch.setattr(sm, "get_skill_manager", lambda: SimpleNamespace(
        get_catalog_skills=lambda **kw: skills))
    out = skills_reply("u1", [])
    assert out.splitlines()[1:3] == ["• digest — The daily digest", "• x-post — Post on X"]
    assert "x-post" in skills_reply("u1", ["post"]) and "digest" not in skills_reply("u1", ["post"])


def test_skills_names_an_unreadable_store(monkeypatch):
    import agents.task.agent.skill_manager as sm
    from surfaces.telegram.skills_ops import skills_reply

    def boom():
        raise OSError("disk")

    monkeypatch.setattr(sm, "get_skill_manager", boom)
    assert "could not read the skills" in skills_reply("u1", [])


# --- /config get | explain -------------------------------------------------- #

def test_config_get_and_explain_read_the_one_describer(monkeypatch, tmp_path):
    import core.runtime_paths as rp
    from surfaces.telegram.prefs_ops import _config_reply
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path))
    out = _config_reply("u1", str(tmp_path), "i", ["get", "delivery.daily_cap"])
    assert out.startswith("delivery.daily_cap = ") and "applies:" in out
    out = _config_reply("u1", str(tmp_path), "i", ["explain", "delivery.daily_cap"])
    assert "Where it comes from" in out
    assert "unknown setting" in _config_reply("u1", str(tmp_path), "i",
                                              ["get", "no.such.key"])


def test_model_write_refuses_both_halves_when_a_higher_layer_pins_the_model(monkeypatch):
    """A layer that pins only DEFAULT_MODEL must not let DEFAULT_PROVIDER land
    alone (a new provider paired with another provider's model)."""
    from core import config_service
    from surfaces.telegram import prefs_ops
    monkeypatch.setattr(config_service, "set_value",
                        lambda *a, **k: pytest.fail("must not write either half"))
    monkeypatch.setattr(config_service, "shadowed_write_refusal",
                        lambda key, value: ("nothing written: `DEFAULT_MODEL` is pinned"
                                            if key == "DEFAULT_MODEL" else None))
    out = prefs_ops.model_reply("u1", ["openai/gpt-5"])
    assert out.startswith("❌") and "DEFAULT_MODEL" in out
