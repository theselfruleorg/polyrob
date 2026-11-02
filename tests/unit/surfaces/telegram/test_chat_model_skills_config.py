"""080 D8 — Telegram parity: /model, /skills, /config get|explain, and ONE
complete verb table (the REPL-only verbs are rows with ``seats=("repl",)``)."""
from types import SimpleNamespace

import pytest



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


# --- /model (094: the live switch; the env pin is never written) ------------- #

def test_model_verb_runs_the_shared_pipeline_with_the_chat_key(monkeypatch):
    import agents.task.model_switch_service as svc
    from core import config_service
    from surfaces.telegram import admin_verbs
    seen = {}

    async def fake(task_agent, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(svc, "model_command", fake)
    monkeypatch.setattr(config_service, "set_value",
                        lambda *a, **k: pytest.fail("/model must not write the env"))
    result = SimpleNamespace(
        decision=SimpleNamespace(session_key="tg:dm:1", session_id="s-1"),
        inbound=SimpleNamespace(identity=SimpleNamespace(
            source=SimpleNamespace(surface_id="telegram"))))
    call = admin_verbs.AdminCall(task_agent=object(), result=result, cmd="/model",
                                 user_id="u1", data_dir="", instance_id="i",
                                 args=["sonnet"], board=None)
    import asyncio
    assert asyncio.run(admin_verbs.handler_for("/model")(call)) == "ok"
    assert seen == {"user_id": "u1", "words": ["sonnet"], "chat_key": "tg:dm:1",
                    "session_id": "s-1", "seat": "telegram"}


def test_model_verb_failure_is_a_reply_not_a_crash(monkeypatch):
    import asyncio
    import agents.task.model_switch_service as svc
    from surfaces.telegram import prefs_ops

    async def boom(task_agent, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(svc, "model_command", boom)
    out = asyncio.run(prefs_ops.model_reply(object(), "u1", ["sonnet"]))
    assert out.startswith("❌") and "Nothing changed" in out


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
