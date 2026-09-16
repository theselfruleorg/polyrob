"""`polyrob wallet asset` — the ONE writer of an operator asset row (046)."""
import pytest
from click.testing import CliRunner

from core.payments import assets


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _invoke(args, **kw):
    from cli.commands.wallet import wallet_cmd
    return CliRunner().invoke(wallet_cmd, ["asset", *args], **kw)


def test_add_refuses_a_chain_the_wallet_registry_does_not_know(home):
    res = _invoke(["add", "--id", "x", "--chain", "atlantis",
                   "--address", "0x" + "ab" * 20, "--decimals", "18",
                   "--symbol", "X"])
    assert res.exit_code != 0
    assert "atlantis" in res.output
    assert "chain" in res.output.lower()


def test_add_refuses_a_malformed_address(home):
    res = _invoke(["add", "--id", "x", "--chain", "base",
                   "--address", "nothex", "--decimals", "18", "--symbol", "X"])
    assert res.exit_code != 0
    assert "address" in res.output.lower()


def test_add_without_decimals_and_without_verify_refuses(home):
    """⚠️ Never guess decimals. They denominate money."""
    res = _invoke(["add", "--id", "rob", "--chain", "robinhood",
                   "--address", "0x" + "ab" * 20, "--symbol", "ROB"])
    assert res.exit_code != 0
    assert "decimals" in res.output.lower()


def test_add_writes_the_declared_values(home):
    res = _invoke(["add", "--id", "rob", "--chain", "robinhood",
                   "--address", "0x" + "ab" * 20, "--decimals", "18",
                   "--symbol", "ROB", "--min-amount", "1000000000000000000"])
    assert res.exit_code == 0, res.output
    row = assets.resolve("rob", data_home=home)
    assert row.decimals == 18 and row.symbol == "ROB"
    assert row.min_amount_raw == 10 ** 18
    assert row.rail == "onchain_scan"
    assert row.source == "operator"


def test_list_names_builtin_and_operator_rows_with_their_source(home):
    _invoke(["add", "--id", "rob", "--chain", "robinhood",
             "--address", "0x" + "ab" * 20, "--decimals", "18", "--symbol", "ROB"])
    res = _invoke(["list"])
    assert res.exit_code == 0, res.output
    assert "usdc-base" in res.output and "builtin" in res.output
    assert "rob" in res.output and "operator" in res.output


def _rpc_ok(decimals_hex="12", symbol_hex="524f42", symbol_len="03"):
    def rpc(method, params):
        if method == "eth_getCode":
            return "0x60806040"
        data = params[0]["data"]
        if data == "0x313ce567":
            return "0x" + "0" * (64 - len(decimals_hex)) + decimals_hex
        if data == "0x95d89b41":
            return ("0x" + "0" * 62 + "20" + "0" * 62 + symbol_len
                    + symbol_hex.ljust(64, "0"))
        raise AssertionError(f"unexpected call {data}")
    return rpc


def test_verify_on_chain_freezes_what_the_contract_reported():
    decimals, symbol = assets.verify_on_chain(
        "base", "0x" + "ab" * 20, rpc_call=_rpc_ok())
    assert decimals == 18
    assert symbol == "ROB"


def test_verify_on_chain_refuses_an_address_with_no_code():
    with pytest.raises(ValueError, match="no code"):
        assets.verify_on_chain("base", "0x" + "ab" * 20,
                               rpc_call=lambda m, p: "0x")


def test_verify_on_chain_refuses_an_absurd_decimals_value():
    with pytest.raises(ValueError, match="decimals"):
        assets.verify_on_chain("base", "0x" + "ab" * 20,
                               rpc_call=_rpc_ok(decimals_hex="ff"))


def test_add_with_verify_uses_the_chain_values_not_the_declared_ones(home):
    res = _invoke(["add", "--id", "rob", "--chain", "robinhood",
                   "--address", "0x" + "ab" * 20, "--decimals", "6",
                   "--symbol", "WRONG", "--verify"],
                  obj={"rpc_call": _rpc_ok()})
    assert res.exit_code == 0, res.output
    row = assets.resolve("rob", data_home=home)
    assert row.decimals == 18 and row.symbol == "ROB"


def test_verify_does_not_overwrite_a_frozen_row_on_a_differing_read(home):
    """⚠️ A later differing read is not an update. Money math reads the frozen
    value; this only REPORTS that the token changed its story."""
    _invoke(["add", "--id", "rob", "--chain", "robinhood",
             "--address", "0x" + "ab" * 20, "--decimals", "18", "--symbol", "ROB"])
    res = _invoke(["verify", "rob"], obj={"rpc_call": _rpc_ok(decimals_hex="06")})
    assert "metadata_changed" in res.output
    assert assets.resolve("rob", data_home=home).decimals == 18


def test_verify_of_an_unknown_asset_refuses(home):
    res = _invoke(["verify", "nope"], obj={"rpc_call": _rpc_ok()})
    assert res.exit_code != 0
    assert "nope" in res.output
