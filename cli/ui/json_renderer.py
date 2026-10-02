"""Versioned one-shot results and normalized event JSONL for shell consumers."""
import json
from dataclasses import asdict

from cli.ui.plain_renderer import PlainRenderer
from cli.ui.events import SessionDone, SessionStart, Step


def _scrub_leaves(value):
    """Scrub every STRING leaf, never the serialized document (CLI8).

    The scrub used to run over ``json.dumps(record)``: a redaction that swallowed a
    closing quote broke the JSON, and the event was lost. Per-leaf scrubbing keeps
    the document well-formed whatever a redaction consumes.
    """
    from cli.ui.secrets import scrub_secrets
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub_leaves(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_leaves(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_secrets(str(value))


class JsonRenderer(PlainRenderer):
    def __init__(self, state, stream, mode):
        super().__init__(state, stream=stream, one_shot=True)
        self.mode = mode
        self.session_id = ""
        self.answer = ""
        self.error = ""
        #: CLI9: the ``send_message`` replies, in order — the agent's ONLY reply
        #: channel. ``done()`` text is bookkeeping and only a fallback.
        self.messages = []

    def _record(self, record):
        print(json.dumps(record, ensure_ascii=False, default=str), file=self._stream, flush=True)

    def _write(self, line):
        pass  # presentation output belongs on stderr, never in the JSON stream

    def on_event(self, event):
        if isinstance(event, SessionStart):
            self.session_id = str((event.raw or {}).get("session_id") or self.session_id)
        if isinstance(event, Step):
            from cli.ui import dialog
            text = dialog.find_message_text(event.actions)
            if text and (not self.messages or self.messages[-1] != text):
                self.messages.append(text)
        if isinstance(event, SessionDone):
            self.answer = event.final_result or self.answer
            self.error = event.error_message or self.error
        if self.mode == "jsonl":
            # Keep the normalized schema and scrub before serialization. Raw feed
            # payloads may contain sensitive/provider-specific structures.
            record = asdict(event)
            record.pop("raw", None)
            self._record({"schema_version": 1, "kind": "event", "event": _scrub_leaves(record)})

    def on_turn_start(self, text):
        pass

    def on_turn_end(self, answer):
        from cli.ui.dialog import is_plumbing_string
        if answer and not is_plumbing_string(answer):
            self.answer = self.answer or answer

    def finish(self, code):
        self._record({
            "schema_version": 1, "kind": "result", "session_id": self.session_id,
            "success": code == 0, "exit_code": code,
            "answer": "\n\n".join(self.messages) if self.messages else self.answer,
            "error": self.error or ("Run failed; see stderr." if code else None),
            "usage": {"tokens": self.state.tokens_total, "cost_usd": self.state.cost_estimate_total},
        })
