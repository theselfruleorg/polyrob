"""CLI5 (2026-10-03 audit): the inline approval takes an explicit token only.

The persistent REPL shares ONE input box between chat and a pending approval,
and the approval accepted plain "yes" / "a" — so a chat line decided a gated
action ("always allow"), against the owner rule that chat text is never a
command. The answer is now a slash token (``/once``, ``/deny`` …); plain text
stays chat.
"""
import asyncio

import pytest

from cli.ui.approval_prompt import ApprovalPrompt


async def _pending(prompt):
    task = asyncio.ensure_future(prompt.request("[approval] Allow 'x'? {}\n"
                                                "  [o]nce / [s]ession / [a]lways-allow / [d]eny / [n]ever: "))
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["yes", "y", "a", "always", "once", "o", "deny", "no", "n"])
async def test_plain_words_are_not_an_answer(text):
    prompt = ApprovalPrompt(lambda t: None)
    task = await _pending(prompt)
    assert prompt.submit(text) is False
    assert not task.done()
    prompt.close()
    assert await task == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,decision", [("/once", "once"), ("/session", "session"),
                                           ("/always", "always"), ("/deny", "deny"),
                                           ("/never", "never"), ("/ONCE", "once")])
async def test_slash_tokens_answer(text, decision):
    prompt = ApprovalPrompt(lambda t: None)
    task = await _pending(prompt)
    assert prompt.submit(text) is True
    assert await task == decision


@pytest.mark.asyncio
async def test_a_slash_verb_with_arguments_is_not_an_answer():
    prompt = ApprovalPrompt(lambda t: None)
    task = await _pending(prompt)
    assert prompt.submit("/deny 0xabc") is False
    assert prompt.submit("/status") is False
    prompt.close()
    await task


@pytest.mark.asyncio
async def test_the_prompt_names_the_tokens():
    shown = []
    prompt = ApprovalPrompt(shown.append)
    task = await _pending(prompt)
    assert "/once" in shown[0] and "/deny" in shown[0]
    assert "plain text" in shown[0]
    prompt.close()
    await task
