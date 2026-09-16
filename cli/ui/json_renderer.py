"""Versioned one-shot results and normalized event JSONL for shell consumers."""
import json
from dataclasses import asdict

from cli.ui.plain_renderer import PlainRenderer
from cli.ui.events import SessionDone, SessionStart


class JsonRenderer(PlainRenderer):
    def __init__(self, state, stream, mode):
        super().__init__(state, stream=stream, one_shot=True)
        self.mode = mode
        self.session_id = ""
        self.answer = ""
        self.error = ""

    def _record(self, record):
        print(json.dumps(record, ensure_ascii=False, default=str), file=self._stream, flush=True)

    def _write(self, line):
        pass  # presentation output belongs on stderr, never in the JSON stream

    def on_event(self, event):
        if isinstance(event, SessionStart):
            self.session_id = str((event.raw or {}).get("session_id") or self.session_id)
        if isinstance(event, SessionDone):
            self.answer = event.final_result or self.answer
            self.error = event.error_message or self.error
        if self.mode == "jsonl":
            from cli.ui.secrets import scrub_secrets
            # Keep the normalized schema and scrub before serialization. Raw feed
            # payloads may contain sensitive/provider-specific structures.
            record = asdict(event)
            record.pop("raw", None)
            self._record({"schema_version": 1, "kind": "event", "event": json.loads(scrub_secrets(json.dumps(record, default=str)))})

    def on_turn_start(self, text):
        pass

    def on_turn_end(self, answer):
        from cli.ui.dialog import is_plumbing_string
        if answer and not is_plumbing_string(answer):
            self.answer = self.answer or answer

    def finish(self, code):
        self._record({
            "schema_version": 1, "kind": "result", "session_id": self.session_id,
            "success": code == 0, "exit_code": code, "answer": self.answer,
            "error": self.error or ("Run failed; see stderr." if code else None),
            "usage": {"tokens": self.state.tokens_total, "cost_usd": self.state.cost_estimate_total},
        })
