"""058 T3.4 — the console's Agent overview renders EVERY axis the posture card has.

``webview/pages_new.py`` looks each axis up in ``build_posture_card()`` by env
name. A fifth card row (the builder mode, 058 WS-3) reached every other seat and
was silently dropped here: a key that is not in the page's tuple is never read,
and a typo in one drops the axis with no error. This pins the tuple to the card.
"""
from pathlib import Path

import webview.pages_new as mod
from core.config_policy.posture_card import build_posture_card
from webview.copy import STRINGS, t

_REPO = Path(__file__).resolve().parents[3]

#: The five posture axes an owner steers (trust, mode, loop, compute, builder).
#: The card also carries diagnostics (pause, console posture, knobs) that are
#: not axes and render elsewhere.
_AXIS_ENVS = {"POLYROB_LOCAL", "AUTONOMY_MODE", "AUTONOMY_POSTURE",
              "AGENT_COMPUTE_POSTURE", "AGENT_BUILDER_MODE"}


def test_every_page_axis_is_a_real_card_row():
    card_envs = {r["env"] for r in build_posture_card()}
    page_envs = {env for _key, env in mod._POSTURE_AXES}
    missing = page_envs - card_envs
    assert not missing, f"page axis env(s) not on the posture card: {missing}"
    assert page_envs == _AXIS_ENVS


def test_agent_context_carries_the_builder_axis_and_its_note(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.delenv("APP_SERVICE_BASE_DOMAIN", raising=False)
    posture = mod._posture_axes()
    assert set(posture) >= {"local", "mode", "loop", "compute", "builder"}
    # The raw value arrives as the owner's answer (070 E.23): a clamped `ship`
    # runs as `build`, so the answer is "pages only".
    assert posture["builder"] == t("agent.axis_value.builder_build")
    # A `ship` request with no domain is CLAMPED and the console must say so.
    assert "CLAMPED" in posture["builder_note"]


def test_template_and_script_render_the_builder_axis():
    html = (_REPO / "webview/templates/agent.html").read_text()
    js = (_REPO / "webview/static/app/agent.js").read_text()
    assert 'data-posture_builder="{{ posture.builder }}"' in html
    assert 'data-posture_builder_note="{{ posture.builder_note }}"' in html
    assert 'data-ov_axis_builder_title=' in html
    assert '{ key: "builder", value: "posture_builder" }' in js


#: Every raw value the posture card can emit for the five axes (070 E.23).
#: ``loop`` also reads ``off`` while autonomy is off.
_RAW_VALUES = {
    "local": ("on", "off"),
    "mode": ("autonomous", "supervised"),
    "loop": ("silent", "owner-visible", "full", "off"),
    "compute": (0, 1, 2, 3),
    "builder": ("off", "build", "ship"),
}


def test_the_raw_value_list_matches_the_policy():
    from core.config_policy.autonomy_posture import _AUTONOMY_POSTURES
    assert set(_AUTONOMY_POSTURES) <= set(_RAW_VALUES["loop"])


def test_every_raw_value_has_an_answer():
    unknown = t("agent.axis_value.unknown")
    for axis, values in _RAW_VALUES.items():
        for raw in values:
            key = f"agent.axis_value.{axis}_{str(raw).replace('-', '_')}"
            assert key in STRINGS, key
            assert mod.axis_answer(axis, raw) == STRINGS[key] != unknown


def test_an_unknown_value_reads_not_known():
    unknown = t("agent.axis_value.unknown")
    assert mod.axis_answer("compute", 9) == unknown
    assert mod.axis_answer("mode", "") == unknown
    assert mod.axis_answer("mode", None) == unknown
    assert mod.axis_answer("local", "unknown") == unknown


def test_the_loop_axis_is_no_while_autonomy_is_off():
    assert mod.axis_answer("loop", "silent", autonomy="off") == t("agent.axis_value.loop_off")
    assert mod.axis_answer("loop", "silent", autonomy="on") == t("agent.axis_value.loop_silent")


def test_every_status_section_has_an_owner_word():
    from core.copy import STRINGS as CORE
    from core.status_snapshot import SECTION_ORDER
    missing = [s for s in SECTION_ORDER if f"status.section.{s}" not in CORE]
    assert not missing, f"no owner word for status sections: {missing}"


def test_the_doctor_names_unverified_sections_in_words():
    from webview.pages import _section_words
    assert _section_words(["work (FileNotFoundError: x)", "nosuch (y)"]) == ["goals", "nosuch"]
    assert _section_words([]) == []
