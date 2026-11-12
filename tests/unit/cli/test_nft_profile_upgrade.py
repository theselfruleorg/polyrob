"""``polyrob nft profile upgrade`` (111 D6): print the spec-2 form and the diff; write only with
``go``, only as root, only when both forms parse to the same pins."""
import json
import os

from click.testing import CliRunner

from cli.commands.nft import nft
from core.wallet import collection_registry as CR
from tests.collection_pins import polyrob_profile, profile

ADDR = "0xC011000000000000000000000000000000000001"


def _file(tmp_path, doc):
    path = tmp_path / "agent_nft_collections.json"
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o444)
    return str(path)


def test_it_prints_the_spec2_form_and_a_diff_and_writes_nothing(tmp_path):
    doc = {"profiles": [polyrob_profile(ADDR)]}
    path = _file(tmp_path, doc)
    res = CliRunner().invoke(nft, ["profile", "upgrade", "--path", path])
    assert res.exit_code == 0, res.output
    assert '"spec": "agent-nft-profile/2"' in res.output and '"kind": "erc6551p"' in res.output
    assert '-  "profiles": [' not in res.output and "+" in res.output and "transfer_safe" in res.output
    assert "Nothing was written" in res.output
    assert json.load(open(path)) == doc


def test_go_needs_root(tmp_path, monkeypatch):
    path = _file(tmp_path, {"profiles": [profile(ADDR)]})
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    res = CliRunner().invoke(nft, ["profile", "upgrade", "go", "--path", path])
    assert res.exit_code == 1 and "needs root" in res.output
    assert json.load(open(path))["profiles"][0]["spec"] == CR.SPEC_V1


def test_go_as_root_writes_spec2_keeps_the_old_file_and_the_pins_are_equal(tmp_path, monkeypatch):
    doc = {"profiles": [polyrob_profile(ADDR), profile("0x" + "c2" * 20)]}
    path = _file(tmp_path, doc)
    before = CR.parse(doc)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a: None)
    res = CliRunner().invoke(nft, ["profile", "upgrade", "go", "--path", path])
    assert res.exit_code == 0, res.output
    written = json.load(open(path))
    assert {p["spec"] for p in written["profiles"]} == {CR.SPEC_V2}
    assert CR.parse(written) == before
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o444)
    kept = [n for n in os.listdir(tmp_path) if ".spec1-" in n]
    assert len(kept) == 1 and json.load(open(tmp_path / kept[0])) == doc
    again = CliRunner().invoke(nft, ["profile", "upgrade", "--path", path])
    assert "already agent-nft-profile/2" in again.output


def test_a_file_that_does_not_parse_is_not_upgraded(tmp_path):
    path = _file(tmp_path, {"profiles": [dict(profile(ADDR), spec="agent-nft-profile/9")]})
    res = CliRunner().invoke(nft, ["profile", "upgrade", "--path", path])
    assert res.exit_code == 1 and "cannot be upgraded" in res.output


def test_no_file_is_nothing_to_do(tmp_path):
    res = CliRunner().invoke(nft, ["profile", "upgrade", "--path", str(tmp_path / "none.json")])
    assert res.exit_code == 0 and "nothing to upgrade" in res.output
