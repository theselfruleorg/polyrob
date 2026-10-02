"""CLI8 + CLI9 (2026-10-03 audit): `--output json|jsonl` stays parseable and
reports the agent's REPLY.

CLI8: the scrub ran over the SERIALIZED event, so a redaction that swallowed a
quote broke the JSON and the event was lost. CLI9: ``answer`` was the ``done()``
text (bookkeeping), not the ``send_message`` reply the owner actually received.
"""
import io
import json

from cli.ui.events import SessionDone, Step, ToolExec
from cli.ui.json_renderer import JsonRenderer
from cli.ui.state import SessionState


def _records(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _send(text):
    return Step(step=1, actions=[{"action_type": "send_message", "name": "message",
                                  "params": {"text": text}}])


def test_answer_is_the_send_message_reply():
    out = io.StringIO()
    r = JsonRenderer(SessionState(), out, "json")
    r.on_event(_send("Here is the summary you asked for."))
    r.on_event(SessionDone(final_result="Task complete: replied to the owner."))
    r.on_turn_end("Task complete: replied to the owner.")
    r.finish(0)
    assert _records(out)[-1]["answer"] == "Here is the summary you asked for."


def test_answer_falls_back_to_done_text_without_a_reply():
    out = io.StringIO()
    r = JsonRenderer(SessionState(), out, "json")
    r.on_event(SessionDone(final_result="done text"))
    r.finish(0)
    assert _records(out)[-1]["answer"] == "done text"


def test_two_replies_are_both_kept():
    out = io.StringIO()
    r = JsonRenderer(SessionState(), out, "json")
    r.on_event(_send("first"))
    r.on_event(_send("second"))
    r.finish(0)
    assert _records(out)[-1]["answer"] == "first\n\nsecond"


def test_secret_in_an_event_is_scrubbed_and_the_json_survives():
    out = io.StringIO()
    r = JsonRenderer(SessionState(), out, "jsonl")
    nasty = 'password=hunter2"x and Authorization: Bearer abcdefghijklmnop"'
    r.on_event(ToolExec(action_name="shell", result_preview=nasty))
    r.on_event(Step(step=2, reasoning='password: "hunter2hunter2" and more'))
    records = _records(out)          # every line parses
    assert len(records) == 2
    blob = json.dumps(records)
    assert "hunter2" not in blob and "abcdefghijklmnop" not in blob
