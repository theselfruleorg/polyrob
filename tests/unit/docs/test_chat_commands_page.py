"""Contract test: docs/guide/chat-commands.md lists every slash command, from code.

Every verb in the one verb table (``core/verbs.py``) and every REPL command
must appear on the page. The generator-sync half (the page equals the
generator output) needs a private script and lives in
ops/tests/docs/test_chat_commands_generated.py.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "docs" / "guide" / "chat-commands.md"


def test_every_verb_and_repl_command_is_on_the_page():
    from cli.ui.commands.handlers import default_registry
    from core import verbs

    text = PAGE.read_text()
    listed = set(re.findall(r"`(/[a-z?][a-z0-9_]*)", text))
    reg = default_registry()
    cmds = reg.commands() if callable(reg.commands) else reg.commands
    wanted = {v.name for v in verbs.VERB_TABLE}
    wanted |= {"/" + c.name for c in cmds} | {"/" + a for c in cmds for a in c.aliases}
    wanted |= {"/task", "/new"}
    missing = sorted(wanted - listed)
    assert not missing, f"slash commands missing from chat-commands.md: {missing}"


def test_no_work_item_id_on_the_page():
    block = PAGE.read_text().split("BEGIN GENERATED", 1)[1]
    assert not re.search(r"\(\d{3}\)", block), "a work-item id leaked into the page"
