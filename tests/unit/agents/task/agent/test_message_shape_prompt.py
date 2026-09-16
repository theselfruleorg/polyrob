"""The prompt's communication contract (design C2 / plan T3).

Two defects are pinned here:

* F2 — `done(text)` had two contradictory jobs in the SAME prompt ("provides
  summary" / "include what was accomplished" vs "to reply, use done"). A model
  obeying both replies AND files a third-person report.
* F9 — every message-shape rule was CONDITIONAL (bound surface, or autonomous
  session, or a pref that is set), so a plain interactive session got no length
  rule at all and wrote terminal-shaped walls.
"""
import pytest

from agents.task.agent.prompts import SystemPrompt


def _prompt(**kw):
    kw.setdefault("use_native_tools", True)
    return SystemPrompt("actions: send_message, done", **kw).get_system_message().content


# --- F9: the shape rules reach EVERY session -------------------------------

def test_message_shape_is_injected_with_no_surface_and_no_prefs():
    text = _prompt()
    assert "<message-shape>" in text
    assert "Lead with the outcome" in text


def test_message_shape_is_injected_on_a_bound_surface_too():
    text = _prompt(surface={"surface_id": "telegram", "max_message_bytes": 4096,
                            "media_out": True, "supports_interactive_ask": True})
    assert text.count("<message-shape>") == 1


def test_message_shape_is_injected_on_an_autonomous_session_too():
    text = _prompt(autonomous=True)
    assert text.count("<message-shape>") == 1


# --- the budget is one number, from style.verbosity -------------------------

@pytest.mark.parametrize("verbosity,expected", [
    ("terse", "5 lines"),
    ("normal", "15 lines"),
])
def test_the_budget_tracks_style_verbosity(verbosity, expected):
    assert expected in _prompt(verbosity=verbosity)


def test_detailed_verbosity_lifts_the_line_budget():
    text = _prompt(verbosity="detailed")
    assert "5 lines" not in text and "15 lines" not in text


def test_an_unknown_verbosity_falls_back_to_normal():
    assert "15 lines" in _prompt(verbosity="shouty")


# --- F2: done has exactly one job ------------------------------------------

def test_done_is_no_longer_described_as_a_summary():
    text = _prompt(autonomous=True)
    for phrase in ("provides summary", "Include what was accomplished",
                   "Provide detailed completion message"):
        assert phrase not in text, f"contradictory done() framing still present: {phrase}"


def test_send_message_is_named_as_the_speech_verb():
    text = _prompt()
    assert "send_message" in text
    assert "never restate a message you already sent" in text.lower()


# --- the address rule reaches every session --------------------------------

def test_a_filesystem_path_is_not_an_address_on_every_session():
    assert "not an address" in _prompt()


# --- cache stability --------------------------------------------------------

def test_the_prompt_is_byte_stable_across_builds():
    kw = dict(surface={"surface_id": "telegram", "max_message_bytes": 4096,
                       "media_out": True, "supports_interactive_ask": True},
              autonomous=True, verbosity="terse")
    assert _prompt(**kw) == _prompt(**kw)
