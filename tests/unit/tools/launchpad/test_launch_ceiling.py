"""125 S8 (A-5): one launch ceiling, priced from the native spend price.

Both seats (``polyrob wallet launch`` and Telegram ``/launch``) used a hand-coded
$6,000-per-native figure. The ceiling is now ``default_launch_ceiling``: the native
spend price × (buy + launch fee + gas room) × ``send_order.TOKEN_COUNT_ROOM``, at least
$25, and ``None`` when no native price can be read (the seats then refuse).
"""
import asyncio

import pytest


def test_the_ceiling_covers_the_buy_at_the_native_price():
    from tools.launchpad.tool import default_launch_ceiling
    ceiling = default_launch_ceiling(1.0, price_fn=lambda chain: 10_000.0)
    assert ceiling is not None and ceiling >= 10_000.0


def test_a_small_launch_keeps_the_floor():
    from tools.launchpad.tool import default_launch_ceiling
    assert default_launch_ceiling(0.0, price_fn=lambda chain: 1_000.0) == 25.0


@pytest.mark.parametrize("price", [None, 0.0, float("nan")])
def test_no_native_price_is_no_ceiling(price):
    from tools.launchpad.tool import default_launch_ceiling
    assert default_launch_ceiling(1.0, price_fn=lambda chain: price) is None


def test_the_native_price_has_one_reader(monkeypatch):
    """reveal, the launch ceiling and the worth text read ``native_spend_price``."""
    import tools.defi.price_sources as ps
    seen = []
    monkeypatch.setattr(ps, "native_spend_price",
                        lambda chain, price_fn=None: seen.append(chain) or 2_000.0)
    from tools.agent_nft import reveal
    from tools.launchpad.tool import LaunchpadTool, default_launch_ceiling
    assert reveal.native_usd("robinhood", None) == 2_000.0
    assert default_launch_ceiling(1.0) >= 2_000.0
    assert "2,000.00" in LaunchpadTool()._native_worth_text(10 ** 18)
    assert len(seen) == 3


class _Capture:
    params = None

    def __init__(self, *a, **k):
        pass

    async def launchpad_launch(self, params, ctx=None):
        _Capture.params = params
        from tools.base_tool import ActionResult
        return ActionResult(extracted_content="ok")


@pytest.fixture
def capture(monkeypatch):
    import tools.launchpad.tool as lt
    _Capture.params = None
    monkeypatch.setattr(lt, "LaunchpadTool", _Capture)
    return lt


def test_the_cli_seat_passes_the_helper_value(capture, monkeypatch):
    from click.testing import CliRunner
    from cli.commands.wallet import wallet_cmd
    monkeypatch.setattr(capture, "default_launch_ceiling", lambda buy, price_fn=None: 4321.0)
    monkeypatch.setattr("cli.commands.wallet._owner_ctx", lambda: None, raising=False)
    CliRunner().invoke(wallet_cmd, ["launch", "SYM", "Name", "--buy", "1"])
    assert _Capture.params is not None and _Capture.params.max_spend_usd == 4321.0


def test_the_cli_seat_refuses_with_no_native_price(capture, monkeypatch):
    from click.testing import CliRunner
    from cli.commands.wallet import wallet_cmd
    monkeypatch.setattr(capture, "default_launch_ceiling", lambda buy, price_fn=None: None)
    res = CliRunner().invoke(wallet_cmd, ["launch", "SYM", "Name", "--buy", "1"])
    assert _Capture.params is None
    assert "--max-usd" in res.output


def test_the_telegram_seat_passes_the_helper_value(capture, monkeypatch):
    from tools.defi.owner_verbs import token as token_ops
    monkeypatch.setattr(capture, "default_launch_ceiling", lambda buy, price_fn=None: 4321.0)
    asyncio.run(token_ops.launch_reply("owner", ["SYM", "Name", "buy", "1"]))
    assert _Capture.params is not None and _Capture.params.max_spend_usd == 4321.0


def test_the_telegram_seat_refuses_with_no_native_price(capture, monkeypatch):
    from tools.defi.owner_verbs import token as token_ops
    monkeypatch.setattr(capture, "default_launch_ceiling", lambda buy, price_fn=None: None)
    text = asyncio.run(token_ops.launch_reply("owner", ["SYM", "Name", "buy", "1"]))
    assert _Capture.params is None
    assert "native price" in text
