"""Approval input owned by the persistent REPL, cancellation-safe without threads."""
import asyncio


class ApprovalPrompt:
    def __init__(self, emit):
        self.emit = emit
        self.pending = None

    async def request(self, prompt):
        if self.pending is not None:
            self.emit("Another approval is awaiting a decision; this request was denied.")
            return "deny"
        future = asyncio.get_running_loop().create_future()
        self.pending = future
        self.emit(prompt)
        try:
            return await future
        finally:
            self.pending = None

    def submit(self, text):
        if self.pending is None or self.pending.done() or text.startswith("/"):
            return False
        from tools.controller.approval_interactive import _parse_ladder
        if _parse_ladder(text) is None:
            return False
        self.pending.set_result(text)
        return True

    def close(self):
        if self.pending is not None and not self.pending.done():
            self.pending.set_result("deny")
