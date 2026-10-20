"""085 W7: the token deploy path — ASCII names, exact supply, exact display."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.wallet import token_template as T
from tools.defi import spl_deploy_verb as V
from tools.defi.trade_params import DeployTokenParams, SolanaDeployTokenParams


# -- D6: printable ASCII only, EVM and Token-2022 ---------------------------

@pytest.mark.parametrize("name", [
    "Rоb Coin",          # Cyrillic o — a homoglyph
    "Rob⁦Coin",          # bidi isolate (the old filter missed it)
    "Rob\u0085Coin",          # C1 control
    "Rob Coin",          # no-break space
    "Rob \U0001F680",         # emoji
    "山羊",           # CJK: not ASCII, refused under D6
])
def test_a_non_ascii_name_refuses_on_both_rails(name):
    with pytest.raises(T.TokenTemplateError, match="printable ASCII"):
        T.encode_constructor_args(name=name, symbol="OK", decimals=18, supply_raw=1)
    with pytest.raises(ValueError, match="printable ASCII"):
        V._validate_metadata_text(SimpleNamespace(name=name, symbol="OK"))
    with pytest.raises(ValueError, match="printable ASCII"):
        V._validate_metadata_text(SimpleNamespace(name="", symbol=name[:16]))


@pytest.mark.parametrize("name", ["Rob Coin", "R-2 $ROB! (v1.0) ~#", "A"])
def test_an_ordinary_ascii_name_still_mints(name):
    T.encode_constructor_args(name=name, symbol="ROB", decimals=18, supply_raw=1)
    V._validate_metadata_text(SimpleNamespace(name=name, symbol="ROB"))


# -- exact supply -----------------------------------------------------------

def test_the_schema_is_a_plain_string_and_a_number_still_validates():
    """reval: ``anyOf [number, string]`` reached Gemini TYPELESS — its
    converter reads only ``type``. A plain string survives every generator;
    a model that sends a JSON number is still accepted by the validator."""
    for model in (DeployTokenParams, SolanaDeployTokenParams):
        prop = model.model_json_schema()["properties"]["supply"]
        assert prop["type"] == "string" and "anyOf" not in prop
    assert DeployTokenParams(name="A", symbol="A", supply=1000,
                             max_spend_usd=1).supply == Decimal(1000)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini",
                                      "openrouter", "deepseek", "nvidia",
                                      "groq", "default"])
def test_every_provider_generator_emits_a_typed_supply(provider):
    from tools.controller.registry.schema_generators import get_schema_generator
    from tools.controller.registry.schema_sanitizer import sanitize_emitted_tools
    gen = get_schema_generator(provider)
    for model in (DeployTokenParams, SolanaDeployTokenParams):
        action = SimpleNamespace(name="deploy_token", description="deploy",
                                 param_model=model)
        tools = gen.generate_tools_list([action])
        tools = sanitize_emitted_tools(tools, provider)
        assert tools, provider            # never silently tools=0
        blob = json.loads(json.dumps(tools))

        def _find(node):
            if isinstance(node, dict):
                if "supply" in (node.get("properties") or {}):
                    return node["properties"]["supply"]
                for v in node.values():
                    if (hit := _find(v)) is not None:
                        return hit
            elif isinstance(node, list):
                for v in node:
                    if (hit := _find(v)) is not None:
                        return hit
            return None
        prop = _find(blob)
        assert prop is not None and prop.get("type") == "string", (provider, prop)


def test_model_dump_is_json_safe_and_canonical():
    """A Decimal in model_dump() broke ``json.dump`` of the agent history; the
    dump is the exact plain digits, so 1e9 and 1000000000 hash the same."""
    a = DeployTokenParams(name="A", symbol="A", supply="1e9", max_spend_usd=1)
    b = DeployTokenParams(name="A", symbol="A", supply=1000000000, max_spend_usd=1)
    assert a.model_dump()["supply"] == "1000000000" == b.model_dump()["supply"]
    json.dumps(a.model_dump())
    assert a.supply == Decimal(10 ** 9)          # the attribute stays exact
    again = DeployTokenParams(**a.model_dump())
    assert again.supply == a.supply
    from tools.controller.approval_queue import compute_request_hash
    assert (compute_request_hash("deploy_token", a, "u")
            == compute_request_hash("deploy_token", b, "u"))


@pytest.mark.parametrize("text,expected", [
    ("1,000,000", Decimal(10 ** 6)), ("1,234.5", Decimal("1234.5")),
    ("1_000_000", Decimal(10 ** 6)), ("1e9", Decimal(10 ** 9)),
    ("1.5", Decimal("1.5"))])
def test_the_owner_spellings_parse(text, expected):
    assert T.to_exact_supply(text) == expected


@pytest.mark.parametrize("text", ["1,5", "1,00,000", ",100", "1,000,00", "1,,000"])
def test_an_ambiguous_comma_refuses(text):
    with pytest.raises(T.TokenTemplateError, match="not a number"):
        T.to_exact_supply(text)


def test_a_string_supply_parses_exactly():
    p = DeployTokenParams(name="A", symbol="A", supply="123456789.123456789",
                          max_spend_usd=1)
    assert p.supply == Decimal("123456789.123456789")
    assert T.whole_to_raw(p.supply, 18) == 123456789123456789 * 10 ** 9


def test_a_json_number_that_was_rounded_refuses_and_names_the_remedy():
    """The tool-call JSON arrives through json.loads: 123456789.123456789 is
    already 123456789.12345679 before pydantic sees it."""
    args = json.loads('{"name": "A", "symbol": "A", "supply": 123456789.123456789,'
                      ' "max_spend_usd": 1}')
    with pytest.raises(ValueError, match="as a string"):
        DeployTokenParams(**args)
    with pytest.raises(ValueError, match="as a string"):
        SolanaDeployTokenParams(**{**args, "decimals": 9})


@pytest.mark.parametrize("raw,expected", [
    ('1000000000', Decimal(10 ** 9)), ('1e9', Decimal(10 ** 9)),
    ('1.5', Decimal("1.5")), ('"1000000"', Decimal(10 ** 6))])
def test_the_natural_json_numbers_still_parse(raw, expected):
    args = json.loads('{"name": "A", "symbol": "A", "supply": %s, '
                      '"max_spend_usd": 1}' % raw)
    assert DeployTokenParams(**args).supply == expected


def test_more_fractional_digits_than_decimals_refuses():
    with pytest.raises(T.TokenTemplateError, match="fractional digits"):
        T.whole_to_raw(Decimal("1.0000001"), 6)
    assert T.whole_to_raw(Decimal("1.000001"), 6) == 1_000_001
    assert T.whole_to_raw(Decimal("1.50"), 1) == 15     # a trailing zero is not a digit


def test_no_rounding_at_the_default_decimal_precision():
    """28 digits is the Decimal default context; a 34-digit raw supply must
    still be exact."""
    s = "999999999999999.123456789012345678"
    assert T.whole_to_raw(s, 18) == 999999999999999123456789012345678


def test_the_whole_supply_ceiling_holds_on_both_rails():
    with pytest.raises(T.TokenTemplateError, match="sanity ceiling"):
        T.whole_to_raw(T.MAX_WHOLE_SUPPLY + 1, 0)
    with pytest.raises(ValueError, match="sanity ceiling"):
        V._supply_to_raw(T.MAX_WHOLE_SUPPLY + 1, 0)
    assert V._supply_to_raw(T.MAX_WHOLE_SUPPLY, 0) == T.MAX_WHOLE_SUPPLY


def test_solana_refuses_a_raw_supply_at_or_above_two_to_the_64():
    # 18446744073 whole at 9 decimals is 18446744073000000000 >= 2**64.
    with pytest.raises(ValueError, match="u64"):
        V._supply_to_raw("18446744073.709551616", 9)       # exactly 2**64 raw
    assert V._supply_to_raw("18446744073.709551615", 9) == 2 ** 64 - 1


def test_solana_refuses_a_rounded_float_too():
    with pytest.raises(ValueError):
        V._supply_to_raw(123456789.123456789, 9)


# -- exact display ----------------------------------------------------------

@pytest.mark.parametrize("value,text", [
    (1234567, "1234567"), (Decimal("1E+9"), "1000000000"), (1e9, "1000000000"),
    ("0.001", "0.001"), (Decimal("1.500"), "1.5"),
    ("123456789.123456789", "123456789.123456789")])
def test_format_supply_is_exact(value, text):
    assert T.format_supply(value) == text


@pytest.mark.asyncio
async def test_the_telegram_retry_hint_carries_the_exact_supply(monkeypatch):
    from surfaces.telegram import token_ops
    import tools.defi.deploy_verb as dv
    seen = {}

    async def _fake(tool, params, ctx):
        seen["supply"] = params.supply
        return SimpleNamespace(error=None, extracted_content="dry run ok")
    monkeypatch.setattr(dv, "perform_deploy_token", _fake)
    out = await token_ops.deploy_reply("owner", ["ROB", "1234567", "Rob", "Coin"])
    assert seen["supply"] == Decimal(1234567)
    assert "/deploy ROB 1234567 Rob Coin go" in out
    assert "e+06" not in out


@pytest.mark.asyncio
async def test_the_evm_approval_text_shows_the_exact_supply_and_the_name_in_repr(
        monkeypatch):
    """Lead "approval-text-name-mismatch": the name is quoted with repr(), so
    a hidden character would be visible; the supply is not `:g`-rounded."""
    from tests.unit.tools.defi.test_deploy_and_call_verbs import _tool
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1234567,
        max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    text = res.extracted_content
    assert "'ROB' ('Rob Coin')" in text
    assert "supply:   1234567 whole units" in text
    assert "e+06" not in text


def test_the_cli_confirmation_shows_the_exact_supply():
    from click.testing import CliRunner
    from cli.commands.wallet import wallet_cmd
    res = CliRunner().invoke(
        wallet_cmd, ["deploy-token", "ROB", "1234567", "Rob", "Coin",
                     "--chain", "base", "--execute"], input="n\n")
    assert "fixed supply of 1234567 on base" in res.output
    assert "e+06" not in res.output


def test_the_cli_refuses_a_supply_that_is_not_a_number():
    from click.testing import CliRunner
    from cli.commands.wallet import wallet_cmd
    res = CliRunner().invoke(wallet_cmd, ["deploy-token", "ROB", "lots", "Rob"])
    assert res.exit_code != 0
    assert "SUPPLY" in res.output or "supply" in res.output


# -- the chat seats: every spelling parses, and the hint round-trips -------

def _hint_args(out: str):
    line = next(l for l in out.splitlines() if "/deploy " in l and l.rstrip().endswith("go"))
    return line[line.index("/deploy ") + len("/deploy "):].split()[:-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed,expected", [
    ("1e9", Decimal(10 ** 9)), ("1,000,000", Decimal(10 ** 6)),
    ("1_000_000", Decimal(10 ** 6)), ("1.5", Decimal("1.5")),
    ("1234567", Decimal(1234567))])
@pytest.mark.parametrize("chain", ["base", "solana"])
async def test_deploy_parses_and_the_retry_hint_round_trips(
        monkeypatch, typed, expected, chain):
    from surfaces.telegram import token_ops
    import tools.defi.deploy_verb as dv
    import tools.defi.spl_deploy_verb as sv
    seen = []

    async def _fake(tool, params, ctx):
        seen.append(params.supply)
        return SimpleNamespace(error=None, extracted_content="dry run ok")
    monkeypatch.setattr(dv, "perform_deploy_token", _fake)
    monkeypatch.setattr(sv, "perform_solana_deploy_token", _fake)
    extra = ["on", "solana"] if chain == "solana" else []
    out = await token_ops.deploy_reply("owner", ["ROB", typed, "Rob", "Coin"] + extra)
    assert seen == [expected], out
    # The owner taps / pastes the hint: it re-parses to the SAME supply.
    out2 = await token_ops.deploy_reply("owner", _hint_args(out))
    assert seen == [expected, expected], out2


@pytest.mark.asyncio
async def test_the_repl_deploy_verb_rides_the_same_parser(monkeypatch):
    from cli.ui.commands import h_token
    from surfaces.telegram import token_ops
    import inspect
    assert "token_ops.deploy_reply" in inspect.getsource(h_token)
    assert token_ops._exact_positive("1,000,000") == Decimal(10 ** 6)


# -- the launchpad takes a name too: the same rule ---------------------------

def test_a_launchpad_name_uses_the_same_ascii_rule():
    from tests.unit.tools.launchpad.test_pons import _terms
    from tools.launchpad import pons
    creator = "0x" + "11" * 20
    with pytest.raises(ValueError, match="printable ASCII"):
        pons.build_launch(terms=_terms(), name="R\u043eb", symbol="ROB",
                          creator=creator)
    with pytest.raises(ValueError, match="printable ASCII"):
        pons.build_launch(terms=_terms(), name="Rob", symbol="R\u2066B",
                          creator=creator)
    pons.build_launch(terms=_terms(), name="Rob Coin", symbol="ROB",
                      creator=creator)


def test_solana_mints_the_validated_stripped_text():
    name, symbol = V._validate_metadata_text(
        SimpleNamespace(name="  Rob Coin ", symbol=" ROB "))
    assert (name, symbol) == ("Rob Coin", "ROB")
    assert V._validate_metadata_text(
        SimpleNamespace(name="   ", symbol="ROB")) == ("ROB", "ROB")


def test_the_cli_refuses_a_non_ascii_name_before_the_confirm_and_quotes_the_name():
    from click.testing import CliRunner
    from cli.commands.wallet import wallet_cmd
    res = CliRunner().invoke(wallet_cmd, ["deploy-token", "ROB", "1,000,000",
                                          "Rоb", "--execute"], input="y\n")
    assert res.exit_code != 0 and "printable ASCII" in res.output
    assert "Proceed?" not in res.output
    ok = CliRunner().invoke(wallet_cmd, ["deploy-token", "ROB", "1,000,000",
                                         "Rob", "Coin", "--execute"], input="n\n")
    assert "'ROB' ('Rob Coin') with a fixed supply of 1000000" in ok.output
