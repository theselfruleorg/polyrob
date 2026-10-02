"""2026-09-24 owner rule: plain chat text is NEVER parsed as a command.

A command is a slash verb (or a tap token / button). A sentence is chat and
goes to the agent. Two pre-LLM word gates broke this and are retired:

* ``owner_stop_intent`` took any message whose first word was stop/pause/resume
  (…) and acted on it without the agent — "resume where you left off" never
  reached Rob, and "Ok please resume buyback" paused EVERYTHING.
* ``parse_pending_decision`` read a bare "approve"/"reject" as a decision on the
  pending queue.

The deterministic, model-free stop is ``/pause`` (``/halt``). These tests stop
either gate from coming back under its old name or seam.
"""
import importlib

import pytest


@pytest.mark.parametrize("module, name", [
    ("core.surfaces.owner_intent", "owner_stop_intent"),
    ("core.surfaces.owner_intent", "Intent"),
    ("core.surfaces.owner_intent", "STOP_WORDS"),
    ("core.surfaces.owner_admin", "apply_owner_intent"),
    ("core.surfaces.owner_admin", "owner_pause_phrases"),
    ("core.surfaces.owner_admin", "parse_pending_decision"),
    ("surfaces.telegram.harness", "_handle_plain_pending_decision"),
    ("surfaces.telegram.harness", "_INTENT_GATE_KINDS"),
    ("cli.commands.chat", "_owner_pause_gate"),
])
def test_the_plain_word_gates_are_gone(module, name):
    assert not hasattr(importlib.import_module(module), name), f"{module}.{name} is back"


def test_the_telegram_gate_module_is_gone():
    with pytest.raises(ImportError):
        importlib.import_module("surfaces.telegram.owner_intent_gate")


def test_the_pause_phrases_pref_is_gone():
    from core import prefs
    assert "pause.phrases" not in prefs.PREF_SCHEMA


def test_the_repl_turn_takes_no_owner_gate():
    import inspect
    from cli.ui.persistent_loop import run_turn
    assert "owner_gate" not in inspect.signature(run_turn).parameters


def test_the_slash_pause_vocabulary_stays():
    """The slash verbs keep their argument parser — that is not chat."""
    from core.surfaces.owner_intent import parse_pause_args
    assert parse_pause_args(["trading", "for", "6h"]) == (("trading",), 360)
    assert parse_pause_args([]) == (("all",), None)
