"""`/launch` and `/deploy` reach their handlers (042).

A chat verb must join FOUR lists or its handler is dead code: the surface
dispatcher's `_COMMANDS` (else it falls through to the agent as chat text), the
harness `_OWNER_ADMIN_COMMANDS` (else it is not owner-gated), the dispatch
tuple (else nothing calls it), and the help body (else nobody knows it exists).
"""
import pytest

from core.surfaces.dispatcher import _COMMANDS
from surfaces.telegram.harness import _HELP_BODY, _OWNER_ADMIN_COMMANDS
from surfaces.telegram import token_ops

OWNER = "owner-1"


@pytest.mark.parametrize("verb", ["/launch", "/deploy"])
def test_the_verb_is_routable_as_a_command(verb):
    assert verb in _COMMANDS


@pytest.mark.parametrize("verb", ["/launch", "/deploy"])
def test_the_verb_is_owner_gated(verb):
    assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.parametrize("verb", ["/launch", "/deploy"])
def test_the_verb_is_documented(verb):
    assert verb in _HELP_BODY


# ==========================================================================
# Parsing — every branch that fires BEFORE any chain read
# ==========================================================================

@pytest.mark.parametrize("reply_fn", [token_ops.launch_reply, token_ops.deploy_reply])
def test_a_non_owner_is_refused(reply_fn):
    assert "Only the owner" in reply_fn(None, ["ROB"])


def test_launch_with_no_arguments_explains_itself():
    out = token_ops.launch_reply(OWNER, [])
    assert "Usage: /launch" in out
    assert "memecoin" in out


def test_deploy_with_too_few_arguments_explains_itself():
    out = token_ops.deploy_reply(OWNER, ["ROB"])
    assert "Usage: /deploy" in out
    assert "does NOT make the token tradable" in out


def test_a_bad_buy_amount_is_refused_before_anything_runs():
    assert "positive number" in token_ops.launch_reply(
        OWNER, ["ROB", "Rob", "Coin", "buy", "lots"])


def test_a_bad_supply_is_refused_before_anything_runs():
    assert "positive number" in token_ops.deploy_reply(
        OWNER, ["ROB", "-5", "Rob", "Coin"])


def test_launch_quotes_by_default(monkeypatch):
    captured = {}

    class _Tool:
        async def launchpad_launch(self, params, ctx):
            captured["params"] = params
            captured["ctx"] = ctx
            return type("R", (), {"error": None,
                                  "extracted_content": "quoted"})()

    monkeypatch.setattr("tools.launchpad.tool.LaunchpadTool", lambda *a, **k: _Tool())
    out = token_ops.launch_reply(OWNER, ["ROB", "Rob", "Coin", "buy", "0.05"])
    assert captured["params"].dry_run is True
    assert captured["params"].symbol == "ROB"
    assert captured["params"].name == "Rob Coin"
    assert captured["params"].buy_amount == 0.05
    # A genuine owner turn: not forged, not a sub-agent.
    assert captured["ctx"].is_sub_agent is False
    assert "Add `go` to execute" in out


def test_go_executes_and_drops_the_retry_hint(monkeypatch):
    captured = {}

    class _Tool:
        async def launchpad_launch(self, params, ctx):
            captured["params"] = params
            return type("R", (), {"error": None, "extracted_content": "sent"})()

    monkeypatch.setattr("tools.launchpad.tool.LaunchpadTool", lambda *a, **k: _Tool())
    out = token_ops.launch_reply(OWNER, ["ROB", "Rob", "Coin", "go"])
    assert captured["params"].dry_run is False
    assert captured["params"].buy_amount == 0.0
    assert "Add `go`" not in out


def test_deploy_parses_the_chain_and_the_name(monkeypatch):
    captured = {}

    async def _perform(tool, params, ctx):
        captured["params"] = params
        return type("R", (), {"error": None, "extracted_content": "quoted"})()

    monkeypatch.setattr("tools.defi.deploy_verb.perform_deploy_token", _perform)
    token_ops.deploy_reply(OWNER, ["ROB", "1000000000", "Rob", "Coin",
                                   "on", "robinhood"])
    assert captured["params"].chain == "robinhood"
    assert captured["params"].supply == 1_000_000_000
    assert captured["params"].name == "Rob Coin"
    assert captured["params"].dry_run is True


def test_an_empty_report_is_flagged_as_a_bug_not_a_success(monkeypatch):
    """A verb that returns neither an error nor a report must never read as
    'it worked' — the owner would assume the funds are where he expected."""
    async def _perform(tool, params, ctx):
        return type("R", (), {"error": None, "extracted_content": ""})()

    monkeypatch.setattr("tools.defi.deploy_verb.perform_deploy_token", _perform)
    out = token_ops.deploy_reply(OWNER, ["ROB", "1000", "Rob"])
    assert "That is a bug" in out
    assert "do NOT retry" in out


# ==========================================================================
# 042b — solana routing, vanity, and REPL parity
# ==========================================================================

def test_deploy_routes_to_the_SOLANA_verb(monkeypatch):
    captured = {}

    async def _spl(tool, params, ctx):
        captured["params"] = params
        return type("R", (), {"error": None, "extracted_content": "quoted"})()

    monkeypatch.setattr("tools.defi.spl_deploy_verb.perform_solana_deploy_token", _spl)
    out = token_ops.deploy_reply(OWNER, ["ROB", "1000000000", "Rob", "Coin",
                                         "on", "solana"])
    assert captured["params"].symbol == "ROB"
    assert captured["params"].decimals == 9
    assert "on solana go" in out


def test_a_vanity_prefix_reaches_the_EVM_verb(monkeypatch):
    captured = {}

    async def _perform(tool, params, ctx):
        captured["params"] = params
        return type("R", (), {"error": None, "extracted_content": "quoted"})()

    monkeypatch.setattr("tools.defi.deploy_verb.perform_deploy_token", _perform)
    out = token_ops.deploy_reply(OWNER, ["ROB", "1000", "Rob", "vanity", "b0b"])
    assert captured["params"].vanity == "b0b"
    assert "vanity b0b go" in out


def test_vanity_is_refused_on_solana_with_the_reason():
    """A Solana mint address is a keypair, not a hash of its code — there is
    nothing to mine against."""
    out = token_ops.deploy_reply(OWNER, ["ROB", "1000", "Rob", "on", "solana",
                                         "vanity", "b0b"])
    assert "nothing to mine against" in out


@pytest.mark.parametrize("verb", ["launch", "deploy"])
def test_the_REPL_has_the_verb_too(verb):
    """Parity: the same helpers, so the two surfaces cannot drift into
    different answers about what a launch costs."""
    from cli.ui.commands import default_registry

    reg = default_registry()
    names = {getattr(c, "name", None) for c in getattr(reg, "_commands", {}).values()}
    assert verb in names


def test_the_REPL_handler_renders_the_same_helper(monkeypatch):
    from cli.ui.commands import h_token

    seen = {}

    class _Ctx:
        user_id = OWNER
        args = ["ROB", "Rob", "Coin"]

        def emit(self, text, title=None):
            seen["text"], seen["title"] = text, title

    monkeypatch.setattr(token_ops, "launch_reply", lambda uid, args: "HELPER OUTPUT")
    h_token.h_launch(_Ctx())
    assert seen["text"] == "HELPER OUTPUT"
    assert seen["title"] == "launch"
