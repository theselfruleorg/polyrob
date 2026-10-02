"""`polyrob tools enable|disable` (062) — the two writes the seat was missing.

The flag is resolved from the GENERATED flag catalog by the naming convention
every tool flag already follows. That is the point: a hand-written tool->flag
table would be a second SSOT, and writing an invented flag name into a user's
.env is silent breakage dressed as a feature.
"""
import pytest
from click.testing import CliRunner


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _run(args):
    from cli.commands.tools import tools
    return CliRunner().invoke(tools, args)


def test_enable_writes_the_catalog_flag(home):
    res = _run(["enable", "coding"])
    assert res.exit_code == 0, res.output
    assert "CODING_TOOLS_ENABLED=true" in (home / ".env").read_text()


def test_disable_writes_false(home):
    assert _run(["disable", "coding"]).exit_code == 0
    assert "CODING_TOOLS_ENABLED=false" in (home / ".env").read_text()


def test_a_money_tool_refuses_and_names_the_deliberate_path(home):
    res = _run(["enable", "launchpad"])
    assert res.exit_code != 0
    assert "spends money" in res.output
    assert "LAUNCHPAD_ENABLED" in res.output
    assert not (home / ".env").exists(), "a refusal must write nothing"


def test_a_high_impact_tool_warns_but_still_writes(home):
    res = _run(["enable", "coding"])
    assert res.exit_code == 0
    assert "high-impact" in res.output
    assert "sub-agent" in res.output, "say which gates stay closed"


def test_an_unclassified_id_is_refused(home):
    res = _run(["enable", "not-a-tool"])
    assert res.exit_code != 0
    assert "unknown tool" in res.output


def test_a_tool_with_no_single_flag_is_refused(home):
    """`browser` is gated by an installed extra, not a flag. Inventing
    BROWSER_ENABLED would write a key nothing reads."""
    res = _run(["enable", "browser"])
    assert res.exit_code != 0
    assert "no single env flag" in res.output


def test_every_resolved_flag_is_a_real_catalog_row():
    from core.flags_catalog import CATALOG
    from core.tool_capabilities import ids_with
    from core.tool_catalog import enable_flag_for

    known = {row[0] for row in CATALOG}
    for tool_id in sorted(ids_with("high_impact") | ids_with("money")):
        flag = enable_flag_for(tool_id)
        if flag:
            assert flag in known, f"{tool_id} -> {flag} is not a documented flag"
