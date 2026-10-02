"""070 E.1 — one word per concept, counted across every seat.

Eight corpora of owner text (the console copy, the core copy, the verb help,
the setting labels, the Inbox render, the Telegram owner replies, the REPL
owner replies) are matched against ``core.copy.glossary``. Each corpus has a
CEILING equal to today's count of strings with a banned word. A commit that
adds a hit fails ``test_corpus_within_ceiling``; a commit that removes one
fails ``test_ceiling_is_tight`` until it lowers the number — so the counts only
ever fall.

Module corpora read string constants only (the AST rule of
``tests/unit/webview/test_copy_layer_ratchet.py``): docstrings and ``logger.*``
arguments are skipped, and so is any string of three words or fewer.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.copy.glossary import GLOSSARY, banned_hits

_REPO = Path(__file__).resolve().parents[3]

#: Strings with at least one banned word, per corpus. Lower a number in the
#: same commit that removes a hit; never raise one.
CEILINGS = {
    "webview_copy": 79,
    "core_copy": 3,
    "verbs": 24,
    "prefs": 0,
    "inbox_render": 16,
    "telegram_owner": 79,
    "repl_owner": 26,
}

_INBOX_RENDER = ("core/surfaces/inbox_render.py", "core/surfaces/owner_admin.py")
_TELEGRAM = tuple(f"surfaces/telegram/{m}.py" for m in (
    "owner_ops", "rail_ops", "history_ops", "send_ops", "token_ops", "identity_ops"))
_REPL = tuple(f"cli/ui/commands/{m}.py" for m in (
    "h_mode", "h_autonomy", "h_money_verbs", "h_inbox", "h_help"))


def _skipped_nodes(tree) -> set:
    """Docstrings and logger arguments: text no owner reads."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
        if isinstance(node, ast.Call):
            func = node.func
            target = getattr(func, "value", None)
            if (getattr(func, "attr", None) in {"debug", "info", "warning", "error",
                                                "exception", "critical"}
                    and getattr(target, "id", "") in {"logger", "log", "_log", "logging"}):
                for sub in ast.walk(node):
                    out.add(id(sub))
    return out


def _module_strings(paths) -> list:
    """``(file:line, text)`` for every owner-length string constant."""
    out = []
    for rel in paths:
        path = _REPO / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _skipped_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and len(node.value.split()) > 3):
                out.append((f"{rel}:{node.lineno}", node.value, None))
    return out


def _corpus(name: str) -> list:
    """``(where, text, key)`` rows of one corpus."""
    if name == "webview_copy":
        from webview.copy import STRINGS
        return [(k, v, k) for k, v in STRINGS.items()]
    if name == "core_copy":
        from core.copy import STRINGS
        return [(k, v, k) for k, v in STRINGS.items()]
    if name == "verbs":
        from core import verbs
        packs = verbs.pack_verb_names()
        return [(v.name, v.help, None) for v in verbs.VERB_TABLE if v.name not in packs]
    if name == "prefs":
        return []                       # E.26 adds the setting owner labels
    if name == "inbox_render":
        return _module_strings(_INBOX_RENDER)
    if name == "telegram_owner":
        return _module_strings(_TELEGRAM)
    if name == "repl_owner":
        return _module_strings(_REPL)
    raise KeyError(name)


def _hits(name: str) -> list:
    return [(where, hits) for where, text, key in _corpus(name)
            for hits in [banned_hits(text, key)] if hits]


@pytest.mark.parametrize("corpus", sorted(CEILINGS))
def test_corpus_within_ceiling(corpus):
    hits = _hits(corpus)
    assert len(hits) <= CEILINGS[corpus], (
        f"{corpus}: {len(hits)} strings use a banned word (ceiling "
        f"{CEILINGS[corpus]}). Use the glossary word (core/copy/glossary.py):\n"
        + "\n".join(f"  {where}: {words}" for where, words in hits))


@pytest.mark.parametrize("corpus", sorted(CEILINGS))
def test_ceiling_is_tight(corpus):
    hits = len(_hits(corpus))
    assert hits >= CEILINGS[corpus], (
        f"{corpus}: only {hits} strings use a banned word now — lower "
        f"CEILINGS[{corpus!r}] from {CEILINGS[corpus]} to {hits}")


def test_banned_hits_strips_code():
    assert banned_hits("Run `polyrob sessions` to see it") == []
    assert banned_hits("Send /rail drop to remove it") == []
    assert banned_hits("I sent it on {chain}") == []
    assert banned_hits("Type polyrob wallet create first") == []
    assert banned_hits("Usage: /x <session|task>") == []


def test_banned_hits_finds_the_words():
    assert banned_hits("I could not read the session") == ["session"]
    assert banned_hits("Over the daily cap") == ["cap"]
    assert banned_hits("Two tools ran") == ["tool"]
    assert banned_hits("It went on-chain") == ["on-chain"]
    assert banned_hits("chain") == ["chain"]
    # A scoped term applies under its key prefix only.
    assert banned_hits("Took 3 actions", "chat.receipt.done") == ["action"]
    assert banned_hits("Took 3 actions", "agent.title") == []
    assert banned_hits("Took 3 actions") == []


def test_every_term_has_a_word_and_a_ban():
    words = set()
    for term in GLOSSARY:
        assert term.word.strip() and term.banned, term
        assert term.word not in words, f"two terms own {term.word!r}"
        words.add(term.word)
        assert not set(term.banned) & set(term.allowed), term


def test_the_corpora_are_not_vacuous():
    for name in ("webview_copy", "core_copy", "verbs", "inbox_render",
                 "telegram_owner", "repl_owner"):
        assert len(_corpus(name)) >= 10, name
