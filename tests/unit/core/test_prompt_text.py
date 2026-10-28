"""081 WS-3.4 — the system prompt's prose lives in ``data/prompts/system/*.md``.

Pins: the reader's file format; a size ceiling per block file (a new block
needs a row, a ceiling only falls); every text is used by the prompt and every
key the prompt asks for exists; the owner-facing texts pass the owner glossary;
a missing placeholder value fails under pytest.
"""
from __future__ import annotations

import itertools

import pytest

from core import prompt_text
from core.copy.glossary import banned_hits

#: Max chars per block FILE. Lower a row when a block shrinks; a new file needs a
#: row. Raising one is a prompt-cost decision: say why in the commit.
BLOCK_MAX_CHARS = {
    "actions": 150,
    "agency": 700,
    "communication": 2050,
    "communication_contract": 1700,
    "filesystem": 650,
    # 093 C3: the missing-capability rule, owner seats only (~64 tokens).
    "harness": 300,
    "identity": 250,
    "input_format": 350,
    # 093 review: + the owner-seat /mcp add line (owner_add, ~20 tokens).
    "mcp_tools": 1720,
    "message_shape": 1100,
    "owner_instructions": 950,
    "response_format": 1850,
    "rules": 650,
    "security": 950,
    "source_precedence": 3300,
    "subtask_delegation": 650,
    "surface": 2700,
    "using_your_tools": 750,
    "vision": 2000,
    "web_access": 1800,
}

#: Texts whose words the model uses WITH the owner (how to ask, what to offer):
#: they speak the owner's vocabulary (core/copy/glossary.py).
OWNER_FACING = ("surface.owner", "surface.owner_verbs", "communication.owner_moves",
                "message_shape.budget_terse", "message_shape.budget_normal",
                "message_shape.budget_detailed")


def test_every_block_file_has_a_size_ceiling_and_stays_under_it():
    files = {p.stem: p.read_text(encoding="utf-8") for p in prompt_text.SYSTEM_DIR.glob("*.md")}
    assert set(files) == set(BLOCK_MAX_CHARS), (
        f"new block file(s) {sorted(set(files) - set(BLOCK_MAX_CHARS))} need a row; "
        f"stale row(s) {sorted(set(BLOCK_MAX_CHARS) - set(files))}")
    over = {k: (len(v), BLOCK_MAX_CHARS[k]) for k, v in files.items()
            if len(v) > BLOCK_MAX_CHARS[k]}
    assert not over, f"block(s) over their ceiling (chars, max): {over}"


def test_owner_facing_texts_use_the_owner_words():
    hits = {k: banned_hits(prompt_text.STRINGS[k]) for k in OWNER_FACING}
    assert not {k: v for k, v in hits.items() if v}, hits


def test_parts_and_trailing_newline(tmp_path):
    (tmp_path / "blk.md").write_text("head\n<!-- part: one -->\nA {x}\n<!-- part: two -->\n"
                                     "\nlead\n", encoding="utf-8")
    (tmp_path / "plain.md").write_text("{{literal}}\n", encoding="utf-8")
    got = prompt_text.load_blocks(tmp_path)
    assert got == {"blk": "head", "blk.one": "A {x}", "blk.two": "\nlead",
                   "plain": "{{literal}}"}


def test_a_missing_key_or_value_fails_under_pytest():
    with pytest.raises(KeyError):
        prompt_text.prompt_text("no_such_block")
    with pytest.raises(KeyError):
        prompt_text.prompt_text("rules.main")  # needs plan= and max_failures=
    assert "{" in prompt_text.prompt_text("response_format.json")  # {{ is a brace


def _render_matrix(monkeypatch):
    """Render the system prompt across its branches (surfaces, tool sets, MCP,
    native/JSON, leaf, autonomous, vision, verbosity, the routing flags)."""
    import core.config_policy as cp
    from agents.task.agent.prompts import SystemPrompt

    for name in ("prefs_tool_enabled", "message_tool_enabled"):
        monkeypatch.setattr(cp, name, lambda: True)
    monkeypatch.setattr(cp.AutonomyConfig, "owner_doc_writable", staticmethod(lambda: True))
    monkeypatch.setattr(cp.AutonomyConfig, "self_context_writable", staticmethod(lambda: True))
    dm = {"surface_id": "telegram", "max_message_bytes": 4096, "media_out": True,
          "supports_interactive_ask": True, "chat_type": "dm"}
    surfaces = (None, dm, {**dm, "media_out": False, "supports_interactive_ask": False},
                {**dm, "chat_type": "group", "chat_instructions": "be kind"},
                {"surface_id": "chat", "correspondent": True})
    toolsets = (None, [], ["web_fetch", "perplexity", "anysite"],
                ["browser", "web_fetch", "perplexity", "anysite", "goal", "task"])
    mcps = (None, {"srv": ["search_web", "b", "c", "d", "e", "f"]}, {"srv": ["x"]},
            {"srv": []})
    for surface, tools, mcp, native, sub, auto, vision in itertools.product(
            surfaces, toolsets, mcps, (True, False), (False, True), (False, True),
            (True, False)):
        for verbosity in ("terse", "normal", "detailed"):
            SystemPrompt("a", use_native_tools=native, tool_ids=tools, mcp_servers=mcp,
                         sub_agent=sub, autonomous=auto, include_vision=vision,
                         surface=surface, verbosity=verbosity).get_system_message()
    # 093 review: an owner seat with no MCP server gets the /mcp add line.
    SystemPrompt("a", use_native_tools=True, tool_ids=["web_fetch"],
                 owner_seat=True).get_system_message()


def test_every_text_is_used_and_every_used_key_exists(monkeypatch):
    import agents.task.agent.prompts as prompts

    used = set()
    real = prompts._p

    def spy(key, **kw):
        used.add(key)
        return real(key, **kw)

    monkeypatch.setattr(prompts, "_p", spy)
    _render_matrix(monkeypatch)
    import agents.task.constants as constants
    monkeypatch.setattr(constants, "ALLOWED_REASONING_TURNS", 0)
    prompts.SystemPrompt("a", use_native_tools=True, tool_ids=[]).get_system_message()
    used |= {f"message_shape.budget_{k}" for k in ("terse", "normal", "detailed")}  # class attr
    assert used <= set(prompt_text.STRINGS), sorted(used - set(prompt_text.STRINGS))
    unused = sorted(set(prompt_text.STRINGS) - used)
    assert not unused, f"block text no render path reaches: {unused}"
