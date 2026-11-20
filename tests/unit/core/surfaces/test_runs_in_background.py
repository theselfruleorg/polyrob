"""Proposal 125 S14: the background rule is ONE core function both the chat
harness and the console chat box ask (``core.surfaces.cards.runs_in_background``)."""
from core.surfaces.cards import runs_in_background


def test_a_quote_answers_inline_and_a_go_runs_in_the_background():
    assert runs_in_background("/send", "/send 1 usdc to 0xabc go")
    assert not runs_in_background("/send", "/send 1 usdc to 0xabc")


def test_avatar_set_and_push_fetch_off_the_box():
    assert runs_in_background("/avatar", "/avatar set https://example.com/a.png")
    assert runs_in_background("/avatar", "/avatar push")
    assert not runs_in_background("/avatar", "/avatar")


def test_a_verb_with_no_background_word_never_does():
    assert not runs_in_background("/status", "/status go")


def test_the_console_has_no_telegram_private_import():
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[4] / "webview" / "console_commands.py"
    mods = {n.module for n in ast.walk(ast.parse(src.read_text()))
            if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith("surfaces.telegram") for m in mods), mods
