"""Every `polyrob <command>` a shipped string names must be a real CLI command.

2026-10-29: the custody refusal told a user-mode install to run
``polyrob signer install``; there is no ``signer`` group. The user had no way to
know the step did not exist until the shell said so.

Two scans:

* every backticked ``polyrob <word> [<sub> ...]`` in a string constant of the
  production tiers (code, docstrings, remedies);
* every ``polyrob <word>`` (backticked or not) in the remedy tables an owner or
  the agent reads as a fix: ``core.security.host_execution`` remedies and
  ``core.tool_gates.REMEDIES`` / ``REMEDY_ACTORS``.

A word resolves through click (``cli.polyrob.cli.get_command``, the lazy table
included) or is a pack CLI command declared in a first-party ``pack.toml``
(packs are not loaded in a unit test, so only the pack's top-level name is
checked). Subcommands resolve while the command is a ``click.Group``; the first
non-group stops the walk (the rest are arguments).
"""
import ast
import re
import tomllib
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parents[3]
TIERS = ("core", "modules", "agents", "tools", "cron", "polyrob", "surfaces", "api",
         "cli", "webview")

_WORD = r"[a-z][a-z0-9_-]*"
BACKTICKED = re.compile(rf"`(?:sudo )?polyrob ({_WORD}(?: {_WORD})*)")
ANY = re.compile(rf"(?:^|[\s`(])(?:sudo )?polyrob ({_WORD}(?: {_WORD})*)")

#: ``(file suffix, command)`` pairs that name a command which is not real ON
#: PURPOSE. Each needs a reason; do not add a remedy here — fix the remedy.
ALLOWED = {
    # The module docstring quotes the bad instruction an incident gave.
    ("core/owner_remedy.py", "x capture-session"),
    # A generated flag row that says the import surface is a later phase.
    ("core/flags_catalog.py", "auth import"),
}


def _pack_commands() -> set:
    names = set()
    for manifest in ROOT.glob("packs/*/*/pack.toml"):
        data = tomllib.loads(manifest.read_text())
        names.update((data.get("cli") or {}).get("commands") or ())
    return names


@pytest.fixture(scope="module")
def resolver():
    from cli.polyrob import cli
    packs = _pack_commands()

    def resolve(words):
        """None when ``words`` resolve, else the first word that does not."""
        group = cli
        for i, word in enumerate(words):
            sub = group.get_command(click.Context(group), word)
            if sub is None:
                if i == 0 and word in packs:
                    return None
                return word
            if not isinstance(sub, click.Group):
                return None
            group = sub
        return None
    return resolve


def _strings(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _command_refs(text: str, pattern):
    for m in pattern.finditer(text):
        words = m.group(1).split()
        # A truncated catalog cell ("`polyrob cron sc…`"): drop the cut word.
        if text[m.end():m.end() + 1] == "…":
            words = words[:-1]
        if words:
            yield words


def test_every_backticked_polyrob_command_in_shipped_code_exists(resolver):
    bad = []
    for tier in TIERS:
        for path in sorted((ROOT / tier).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for lineno, text in _strings(path):
                for words in _command_refs(text, BACKTICKED):
                    if (rel, " ".join(words)) in ALLOWED:
                        continue
                    missing = resolver(words)
                    if missing:
                        bad.append(f"{rel}:{lineno}: `polyrob {' '.join(words)}` "
                                   f"({missing!r} is not a command)")
    assert not bad, "a shipped string names a CLI command that does not exist:\n" + "\n".join(bad)


def _remedy_texts():
    from core.security import host_execution as he
    from core import tool_gates as tg
    yield "CUSTODY_FILE_REMEDY", he.CUSTODY_FILE_REMEDY
    yield "SNAPSHOT_COPY_REMEDY", he.SNAPSHOT_COPY_REMEDY
    for key, text in tg.REMEDIES.items():
        yield f"REMEDIES[{key}]", text
    for key, actor in tg.REMEDY_ACTORS.items():
        for value in vars(actor).values() if hasattr(actor, "__dict__") else actor:
            if isinstance(value, str):
                yield f"REMEDY_ACTORS[{key}]", value


def test_every_polyrob_command_in_a_remedy_exists(resolver):
    bad = []
    for name, text in _remedy_texts():
        for words in _command_refs(text, ANY):
            missing = resolver(words)
            if missing:
                bad.append(f"{name}: `polyrob {' '.join(words)}` ({missing!r} is not a command)")
    assert not bad, "a remedy names a CLI command that does not exist:\n" + "\n".join(bad)


def test_the_custody_remedy_names_no_signer_verb():
    from core.security.host_execution import CUSTODY_FILE_REMEDY
    assert "polyrob signer" not in CUSTODY_FILE_REMEDY
    assert "`polyrob wallet export`" in CUSTODY_FILE_REMEDY
    assert "CODE_EXEC_BACKEND=docker" in CUSTODY_FILE_REMEDY
