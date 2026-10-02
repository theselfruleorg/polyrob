"""Approval input owned by the persistent REPL, cancellation-safe without threads.

CLI5 (2026-10-03): the answer is an explicit slash token, never a chat word. The
persistent REPL shares ONE input box between chat and a pending approval, and a
plain "yes" or "a" used to decide the gated action — against the owner rule
that chat text is never a command. Plain text now stays chat.
"""
import asyncio

#: The ladder's decisions, each answered by ``/<decision>`` and nothing else. A
#: bare ``/deny`` is the approval's; ``/deny <address>`` stays the allowlist verb.
_TOKENS = ("once", "session", "always", "deny", "never")


def _answer_hint(prompt: str) -> str:
    wide = "[s]ession" in (prompt or "")
    choices = [t for t in _TOKENS if wide or t not in ("session", "always")]
    return ("  answer with " + " · ".join("/" + t for t in choices)
            + " — plain text is chat, never a decision")


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
        self.emit(f"{(prompt or '').rstrip()}\n{_answer_hint(prompt)}")
        try:
            return await future
        finally:
            self.pending = None

    def submit(self, text):
        if self.pending is None or self.pending.done():
            return False
        token = (text or "").strip()
        if not token.startswith("/") or len(token.split()) != 1:
            return False
        decision = token[1:].lower()
        if decision not in _TOKENS:
            return False
        self.pending.set_result(decision)
        return True

    def close(self):
        if self.pending is not None and not self.pending.done():
            self.pending.set_result("deny")
