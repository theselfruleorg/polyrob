"""067 (one install) — the first-party pack helpers in core/lock_closure.py
(stdlib only). The packs ship INSIDE polyrob: a pack is a ``polyrob.packs``
entry point of the core pyproject plus ``packs/<dir>/<package>/pack.toml``, and
choosing a pack chooses the core extra its pack.toml names.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from core import lock_closure as lc

ROOT = Path(__file__).resolve().parents[3]


def _pack(root: Path, dirname: str, pack_id: str, extra="", requires=()):
    d = root / "packs" / dirname / f"polyrob_{pack_id}"
    d.mkdir(parents=True)
    reqs = ", ".join(f'"{r}"' for r in requires)
    (d / "pack.toml").write_text(f'id = "{pack_id}"\nextra = "{extra}"\nrequires_packs = [{reqs}]\n')


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "polyrob"\nversion = "1.1.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["click>=8"]\n'
        '[project.optional-dependencies]\ncrypto = ["web3>=6"]\ntwitter = ["tweepy>=4"]\n'
        'solana = ["solders"]\n'
        '[project.entry-points."polyrob.packs"]\n'
        'wallet = "polyrob_wallet:pack"\ndefi = "polyrob_defi:pack"\nx = "polyrob_x:pack"\n')
    _pack(tmp_path, "wallet", "wallet", "crypto")
    _pack(tmp_path, "defi", "defi", "solana", requires=("wallet",))
    _pack(tmp_path, "x", "x", "twitter")
    return tmp_path


def test_packs_are_the_core_entry_points_with_their_manifests(tree):
    packs = lc.first_party_packs(tree)
    assert [(p.id, p.extra, p.path) for p in packs] == [
        ("defi", "solana", "packs/defi"), ("wallet", "crypto", "packs/wallet"),
        ("x", "twitter", "packs/x")]


def test_selection_all_none_list_and_typo(tree):
    packs = lc.first_party_packs(tree)
    assert [p.id for p in lc.select_packs(packs, "all")] == ["defi", "wallet", "x"]
    assert lc.select_packs(packs, "") == [] and lc.select_packs(packs, "none") == []
    assert [p.id for p in lc.select_packs(packs, "x, wallet")] == ["x", "wallet"]
    with pytest.raises(KeyError, match="unknown pack nope"):
        lc.select_packs(packs, "x,nope")


def test_a_pack_that_requires_a_pack_takes_its_extra_too(tree):
    packs = lc.first_party_packs(tree)
    assert [p.id for p in lc.install_order(packs, lc.select_packs(packs, "defi"))] == ["wallet", "defi"]
    assert lc.pack_extras(packs, lc.select_packs(packs, "defi")) == ["crypto", "solana"]
    assert lc.pack_extras(packs, lc.select_packs(packs, "x")) == ["twitter"]


def test_an_entry_point_without_exactly_one_manifest_or_a_bad_extra_is_refused(tree):
    (tree / "packs/x/polyrob_x/pack.toml").unlink()
    with pytest.raises(ValueError, match="expected one"):
        lc.first_party_packs(tree)
    _pack(tree, "x2", "x", "nope")
    with pytest.raises(ValueError, match="not a polyrob extra"):
        lc.first_party_packs(tree)


def test_the_lock_command_compiles_only_the_core_pyproject(tree):
    assert lc.lock_command(tree) == [
        "uv", "pip", "compile", "pyproject.toml", "--all-extras", "--universal",
        "--python-version", "3.11", "--generate-hashes", "-o", "requirements.lock"]


def test_the_packs_subcommand_prints_ids_and_rows(tree):
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "core/lock_closure.py"), "packs",  # noqa: E731
                                     "--root", str(tree), *a], capture_output=True, text=True)
    out = run("--ids", "defi")
    assert out.returncode == 0 and out.stdout.split() == ["wallet", "defi"], out.stderr
    rows = run("--ids", "all", "--list").stdout.splitlines()
    assert rows[:2] == [f"wallet crypto {tree / 'packs/wallet'}", f"defi solana {tree / 'packs/defi'}"]
    bad = run("--ids", "nope")
    assert bad.returncode == 2 and "unknown pack nope" in bad.stderr


def test_deps_maps_a_pack_to_its_extra():
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    base = lc.project_deps(lock, ROOT / "pyproject.toml", ["server"])
    with_x = lc.project_deps(lock, ROOT / "pyproject.toml", ["server"], "x")
    via_extra = lc.project_deps(lock, ROOT / "pyproject.toml", ["server", "twitter"])
    assert "tweepy==" not in base and "tweepy==" in with_x
    assert "polyrob[server,twitter] (packs [x])" in with_x.splitlines()[0]
    body = lambda t: t.split("\n", 1)[1]  # noqa: E731
    assert body(with_x) == body(via_extra)
    with pytest.raises(KeyError):
        lc.project_deps(lock, ROOT / "pyproject.toml", ["server"], "nope")


def test_the_real_tree_maps_each_first_party_pack_to_its_sdk_extra():
    assert {p.id: p.extra for p in lc.first_party_packs(ROOT)} == {
        "discovery": "anysite", "markets": "crypto", "x": "twitter"}
