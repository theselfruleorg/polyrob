"""`/bridge` must be reachable, owner-gated, and helped (037).

The bridge shipped CLI-only, so the one person allowed to run it had to SSH to
the box — while his standing directive is that he can do anything from the chat,
and he is usually on a phone. A verb that is handled but not ROUTABLE is dead
code (`/halt` shipped that way), so this pins all three lists agree.
"""
import pytest


def test_the_verb_is_routable():
    from core.surfaces.dispatcher import _COMMANDS
    assert "/bridge" in _COMMANDS


def test_the_verb_is_owner_gated():
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    assert "/bridge" in _OWNER_ADMIN_COMMANDS


def test_the_verb_is_in_the_help_ssot():
    """help_commands() feeds Telegram's setMyCommands, so being here is what
    puts the verb in the phone's "/" menu. Names are parsed WITHOUT the slash."""
    from surfaces.telegram.harness import help_commands
    assert "bridge" in {c for c, _ in help_commands()}


@pytest.mark.asyncio
async def test_a_non_owner_is_refused():
    from surfaces.telegram import owner_ops
    assert "Only the owner" in await owner_ops.bridge_reply(
        None, "/tmp", ["solana", "base", "1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [[], ["solana"], ["solana", "base"]])
async def test_incomplete_args_show_usage_not_a_traceback(args):
    """A15: the copy no longer says every bridge needs approval — under the
    autonomous ceiling it runs and reports; only above it does it wait in
    /pending (spend_lane.py DEFI_SPEND_VERBS, 2026-09-12)."""
    from surfaces.telegram import owner_ops
    out = await owner_ops.bridge_reply("rob", "/tmp", args)
    assert "Usage: /bridge" in out
    assert "pending" in out.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "-1", "0"])
async def test_a_bad_amount_is_refused_before_any_quote(bad):
    from surfaces.telegram import owner_ops
    out = await owner_ops.bridge_reply("rob", "/tmp", ["solana", "robinhood", bad])
    assert "positive number" in out


@pytest.mark.asyncio
async def test_the_bare_form_is_a_QUOTE_not_an_execution(monkeypatch):
    """Typing the verb must never move funds. `go` is the deliberate second act."""
    seen = {}

    async def _fake(tool, params, ctx):
        seen["dry_run"] = params.dry_run
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.defi.bridge_verb.perform_bridge", _fake)
    from surfaces.telegram import owner_ops
    out = await owner_ops.bridge_reply("rob", "/tmp", ["solana", "robinhood", "0.9"])
    assert seen["dry_run"] is True
    assert "Add `go` to execute" in out


@pytest.mark.asyncio
async def test_go_executes(monkeypatch):
    seen = {}

    async def _fake(tool, params, ctx):
        seen["dry_run"] = params.dry_run
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.defi.bridge_verb.perform_bridge", _fake)
    from surfaces.telegram import owner_ops
    await owner_ops.bridge_reply("rob", "/tmp", ["solana", "robinhood", "0.9", "go"])
    assert seen["dry_run"] is False


@pytest.mark.asyncio
async def test_an_empty_report_never_reads_as_nothing_happened(monkeypatch):
    async def _fake(tool, params, ctx):
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="")

    monkeypatch.setattr("tools.defi.bridge_verb.perform_bridge", _fake)
    from surfaces.telegram import owner_ops
    out = await owner_ops.bridge_reply("rob", "/tmp", ["solana", "robinhood", "0.9"])
    assert "do NOT retry" in out


@pytest.mark.asyncio
async def test_the_chain_list_comes_from_the_registry(monkeypatch):
    """C62: the usage line names the chains this build KNOWS.

    A hand-typed list of money chains can only drift from
    ``core.wallet.chains``, and a wrong one sends the owner at a chain the
    bridge will refuse.
    """
    from core.wallet import chains
    from surfaces.telegram import owner_ops
    out = await owner_ops.bridge_reply("rob", "/tmp", [])
    for name in chains.names():
        assert name in out


@pytest.mark.asyncio
async def test_as_names_the_destination_asset(monkeypatch):
    """C63: `as <asset>` reaches ``BridgeParams.token_out``.

    Without it every chat bridge arrived as NATIVE, and the field the rail has
    carried all along was unreachable from the seat the owner actually uses.
    """
    seen = {}

    async def _fake(tool, params, ctx):
        seen["token_out"] = params.token_out
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.defi.bridge_verb.perform_bridge", _fake)
    from surfaces.telegram import owner_ops
    await owner_ops.bridge_reply("rob", "/tmp",
                                 ["solana", "base", "0.9", "as", "usdc", "go"])
    assert seen["token_out"] == "usdc"
