"""A skill write must make the next ``load_skill`` re-emit the body (2026-09-21).

``skill_manage``'s CAS failure says ``revision conflict; reload before editing``.
``load_skill`` is the only reload verb, and ``build_load_skill_result``
short-circuits an id already in ``Controller._activated_skills`` with "already
active this session — no need to reload" — a token saver that made the error's
own remedy a no-op. Prod, 2026-09-21 15:35:49, the agent's own words:

    load_skill returned 'already active this session — no need to reload',
    so the revision was not refreshed by that call.

The same stale cache also hid an approved edit from the session that made it.
"""
import logging

from types import SimpleNamespace

from tools.controller._helpers import build_load_skill_result, forget_activated_skill


def _controller():
    c = SimpleNamespace(logger=logging.getLogger("reload-test"),
                        _activated_skills=set())
    return c


def test_load_skill_short_circuits_without_a_bust():
    """The cache that caused the bug is still there (this is the baseline)."""
    c = _controller()
    skills = {"demo": SimpleNamespace(content="V1")}
    first = build_load_skill_result(skills, "demo", activated=c._activated_skills)
    assert "V1" in (first.extracted_content or "")
    skills["demo"] = SimpleNamespace(content="V2")
    again = build_load_skill_result(skills, "demo", activated=c._activated_skills)
    assert again.metadata.get("skill_already_active") is True
    assert "V2" not in (again.extracted_content or "")


def test_forget_activated_skill_makes_the_next_load_re_emit_the_new_body():
    c = _controller()
    skills = {"demo": SimpleNamespace(content="V1")}
    build_load_skill_result(skills, "demo", activated=c._activated_skills)
    skills["demo"] = SimpleNamespace(content="V2")
    forget_activated_skill(c, "demo")          # what skill_manage now does on write
    after = build_load_skill_result(skills, "demo", activated=c._activated_skills)
    assert "V2" in (after.extracted_content or "")
    assert after.metadata.get("skill_loaded") == "demo"


def test_forget_activated_skill_is_fail_open_and_id_normalised():
    forget_activated_skill(SimpleNamespace(), "demo")           # no set at all
    forget_activated_skill(SimpleNamespace(_activated_skills=None), "demo")
    c = _controller()
    c._activated_skills.add("demo")
    forget_activated_skill(c, '  "demo" ')                      # quoted/padded id
    assert "demo" not in c._activated_skills


def test_skill_manage_busts_the_cache_on_every_successful_write():
    """The wiring, not the helper: the call site must exist in skill_manage."""
    import inspect

    from tools.controller import action_registration as ar
    src = inspect.getsource(ar.ActionRegistrationMixin._register_skill_manage_action)
    # create/patch share a call, delete has its own; model promotion is refused.
    assert src.count("forget_activated_skill(self, params.skill_id)") == 2
