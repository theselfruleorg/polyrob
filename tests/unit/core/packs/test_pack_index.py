"""The curated pack index, the kill list and third-party install (067 P7)."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.packs import index, state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _install, _toml

#: the first-party packs move with the core version (every pack.toml pin moves at a cut)
_FP = {r["id"]: r["version"] for r in json.loads(
    (Path(__file__).resolve().parents[4] / "core" / "packs" / "index.json").read_text())}

REPO = Path(__file__).resolve().parents[4]


def _kill(tmp_path, monkeypatch, *rows):
    path = tmp_path / "removed.json"
    path.write_text(json.dumps(list(rows)))
    monkeypatch.setattr(index, "REMOVED_FILE", path)
    return path


def _row(pid="echo", versions=">=0", dist=None):
    row = {"id": pid, "versions": versions, "reason": "exfiltrates keys", "date": "2026-09-24"}
    if dist:
        row["dist"] = dist
    return row


# --- the files -------------------------------------------------------------------

def test_the_index_and_kill_list_are_generated_and_mirrored_into_core():
    if (REPO / "scripts/gen_pack_index.py").is_file():  # scripts/ is not in the public export
        out = subprocess.run([sys.executable, "scripts/gen_pack_index.py", "--check"], cwd=REPO,
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
    for name in ("index.json", "removed.json"):
        assert (REPO / "packs" / name).read_bytes() == (REPO / "core/packs" / name).read_bytes()


def test_every_in_repo_pack_has_a_first_party_row():
    """067 (one install): the in-repo packs are the core pyproject's
    polyrob.packs entry points; each row's distribution is polyrob itself."""
    import tomllib
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    eps = project["entry-points"]["polyrob.packs"]
    first = [r for r in index.rows() if r["tier"] == "first-party"]
    assert {r["id"]: r["entry_point"] for r in first} == eps
    assert {r["dist"] for r in first} == {"polyrob"}
    manifests = {p.parent.name for p in (REPO / "packs").glob("*/*/pack.toml")}
    assert manifests == {v.partition(":")[0] for v in eps.values()}
    assert not list((REPO / "packs").glob("*/pyproject.toml")), \
        "a first-party pack is not a second distribution (067, one install)"


def test_first_party_identity_is_the_index():
    from core.packs.loader import _FIRST_PARTY
    ids = index.first_party_identities()
    assert ids["discovery"] == ("polyrob", "polyrob_discovery:pack")
    assert ids["x"] == ("polyrob", "polyrob_x:pack")
    assert {k: v for k, v in _FIRST_PARTY.items() if k in ids} == ids


def test_a_third_party_row_must_pin_a_wheel_hash():
    row = {"id": "t", "dist": "t", "version": "1", "tier": "third-party", "entry_point": "t:pack"}
    with pytest.raises(index.PackIndexError):
        index.validate_row(row)
    index.validate_row({**row, "sha256": "a" * 64})


def test_the_kill_list_rows_are_validated():
    with pytest.raises(index.PackIndexError):
        index.validate_removed(_row(versions="not a spec"))
    with pytest.raises(index.PackIndexError):
        index.validate_removed({**_row(), "date": "yesterday"})


# --- killed() --------------------------------------------------------------------

def test_killed_matches_id_or_dist_and_the_version_range(tmp_path, monkeypatch):
    _kill(tmp_path, monkeypatch, _row("evil", "<2", dist="Evil_Pack"))
    assert "exfiltrates keys" in index.killed("evil", "1.0")
    assert index.killed("evil-pack", "1.5")
    assert index.killed("evil", "2.0") is None
    assert index.killed("evil", None)            # unknown version fails closed
    assert index.killed("other", "1.0") is None


def test_an_unreadable_kill_list_is_not_an_empty_one(tmp_path, monkeypatch):
    path = tmp_path / "removed.json"
    path.write_text("{broken")
    monkeypatch.setattr(index, "REMOVED_FILE", path)
    assert "unreadable" in index.killed("anything", "1.0")


def test_the_shipped_kill_list_starts_empty():
    assert index.removed_rows() == []


# --- the loader ------------------------------------------------------------------

def test_phase_one_refuses_a_killed_pack_before_import(use_packs, tmp_path, monkeypatch):
    _kill(tmp_path, monkeypatch, _row("echo"))
    loader = use_packs("echo")
    loader.load_packs()
    rec = state.record("echo")
    assert rec.status == state.REFUSED and "kill list" in rec.reason
    assert "polyrob_echo" not in sys.modules
    assert state.pack_of_tool("echo") is None     # no policy rows registered


def test_phase_one_matches_the_installed_distribution(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "echo2", _toml("echo2", tier="third-party"))
    _kill(tmp_path, monkeypatch, _row("unrelated", "==0.9", dist="polyrob-echo2"))
    ep = SimpleNamespace(name="echo2", value="polyrob_echo2:pack", module="polyrob_echo2",
                         dist=SimpleNamespace(metadata={"Name": "polyrob-echo2"}, version="0.9"))
    monkeypatch.setattr(scratch, "_entry_points", lambda: [ep])
    scratch.register_policies()
    assert "kill list" in state.record("echo2").reason


def test_phase_two_rechecks_the_kill_list_before_import(use_packs, tmp_path, monkeypatch):
    loader = use_packs("echo")
    loader.register_policies()
    assert state.record("echo").status == state.INSTALLED
    _kill(tmp_path, monkeypatch, _row("echo", "==0.1.0"))
    loader.load_packs()
    rec = state.record("echo")
    assert rec.status == state.REFUSED and "kill list" in rec.reason
    assert "polyrob_echo" not in sys.modules


# --- the CLI ---------------------------------------------------------------------

@pytest.fixture
def cli(use_packs, monkeypatch, tmp_path):
    import cli.commands._bootstrap as boot
    monkeypatch.setattr(boot, "_env_loaded", True)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    use_packs("echo").register_policies()
    from cli.polyrob import cli as root
    return lambda *args, **kw: CliRunner().invoke(root, ["pack", *args], **kw)


def test_search_reads_the_local_index(cli, tmp_path, monkeypatch):
    r = cli("search")
    assert r.exit_code == 0 and f"discovery {_FP['discovery']} (first-party)" in r.output and f"x {_FP['x']}" in r.output
    r = cli("search", "twitter")
    assert f"x {_FP['x']}" in r.output and "discovery" not in r.output
    _kill(tmp_path, monkeypatch, _row("x"))
    assert "! on the pack kill list" in cli("search", "x").output
    assert "no pack in the index matches" in cli("search", "zzz-none").output


def test_enable_refuses_a_killed_pack(cli, tmp_path, monkeypatch):
    rec = state.record("echo")
    _kill(tmp_path, monkeypatch, _row("echo"))
    r = cli("enable", "echo")
    assert r.exit_code != 0 and "cannot be enabled" in r.output and "kill list" in r.output
    assert rec is not None


def test_install_resolves_only_index_ids(cli):
    r = cli("install", "wallet")
    assert r.exit_code != 0 and "not in the pack index" in r.output
    r = cli("install", "bad;id")
    assert r.exit_code != 0 and "is not a pack id" in r.output


def _first_party_state(monkeypatch, pack_id, present):
    """The real pack.toml of *pack_id*, loaded, with its SDK probe forced."""
    import cli.commands.pack as pack_cmd
    import core.packs.sdk as sdk
    from core.packs.manifest import read
    rec = state.PackRecord(id=pack_id, entry_point=f"polyrob_{pack_id}:pack",
                           manifest=read(f"polyrob_{pack_id}"), status=state.LOADED,
                           dist_name="polyrob", dist_version="1.1.0")
    fake = SimpleNamespace(record=lambda pid: rec if pid == pack_id else None,
                           LOADED=state.LOADED)
    monkeypatch.setattr(pack_cmd, "_loaded_state", lambda: fake)
    monkeypatch.setattr(sdk, "_present", lambda m: present)
    return rec


def test_first_party_install_names_the_extra_never_a_pack_distribution(cli, monkeypatch):
    """067 (one install): a first-party pack ships inside polyrob; its install is
    its SDK extra. The retired separate distribution name is never printed."""
    from core.packs.index import RETIRED_DISTS
    _first_party_state(monkeypatch, "x", present=False)
    r = cli("install", "x")
    assert r.exit_code == 1, r.output
    assert "ships inside polyrob" in r.output
    assert "pip install 'polyrob[twitter]'" in r.output
    assert "install.sh --packs x" in r.output
    assert "twitter" in r.output.split("tools withheld:")[1]
    assert not any(name in r.output for name in RETIRED_DISTS)


def test_first_party_install_of_a_complete_pack_is_a_no_op(cli, monkeypatch):
    _first_party_state(monkeypatch, "markets", present=True)
    r = cli("install", "markets")
    assert r.exit_code == 0 and "is complete" in r.output, r.output


def test_first_party_install_uses_the_trusted_lazy_closure_of_its_extra(cli, monkeypatch):
    import core.lazy_deps as lazy
    called = []
    monkeypatch.setattr(lazy, "ensure", lambda feature: called.append(feature))
    _first_party_state(monkeypatch, "discovery", present=False)
    r = cli("install", "discovery")
    assert r.exit_code == 0, r.output
    assert called == ["tool.anysite"]


def test_path_install_refuses_a_second_entry_point(cli, pip_calls, tmp_path):
    src = _third_party_source(tmp_path / "two")
    py = src / "pyproject.toml"
    py.write_text(py.read_text() + 'markets = "shadow:pack"\n')
    r = cli("install", str(src), "--accept-capabilities")
    assert r.exit_code != 0 and "more than one polyrob.packs entry" in r.output, r.output
    assert pip_calls == []


def test_install_refuses_a_killed_index_pack(cli, tmp_path, monkeypatch):
    _kill(tmp_path, monkeypatch, _row("x", f"=={_FP['x']}"))
    r = cli("install", "x")
    assert r.exit_code != 0 and "kill list" in r.output


def _third_party_source(root: Path, pid="thirdy", tier="third-party") -> Path:
    pkg = root / f"polyrob_{pid}"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise SystemExit('pack code must not run at install')\n")
    (pkg / "pack.toml").write_text(_toml(pid, tier=tier))
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "polyrob-{pid}"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2"]\n'
        f'[project.entry-points."polyrob.packs"]\n{pid} = "polyrob_{pid}:pack"\n')
    return root


@pytest.fixture
def pip_calls(monkeypatch):
    import cli.commands.pack as pack_cmd
    calls = []
    monkeypatch.setattr(pack_cmd, "_pip", lambda args: calls.append(list(args)))
    monkeypatch.setattr(pack_cmd, "_refuse_under_custody", lambda: None)
    return calls


SHA = "ab" * 20


@pytest.fixture
def fake_clone(monkeypatch):
    import cli.commands.skill_install as si
    seen = {}

    def clone(url, dest, *, ref=None, sha=None):
        seen.update(url=url, sha=sha)
        _third_party_source(Path(dest))
        return sha
    monkeypatch.setattr(si, "clone_audited", clone)
    return seen


def test_git_install_requires_a_pinned_commit(cli, pip_calls):
    for spec in ("git+https://example.com/p.git", "git+https://example.com/p.git@main",
                 "git+ssh://example.com/p.git@" + SHA):
        r = cli("install", spec)
        assert r.exit_code != 0 and "40-hex" in r.output, spec
    assert pip_calls == []


def test_git_install_shows_capabilities_and_requires_acceptance(cli, pip_calls, fake_clone):
    r = cli("install", f"git+https://example.com/p.git@{SHA}")
    assert r.exit_code != 0 and "--accept-capabilities" in r.output
    assert "declared capabilities: tools" in r.output and "tool thirdyt" in r.output
    assert pip_calls == []
    r = cli("install", f"git+https://example.com/p.git@{SHA}", "--accept-capabilities")
    assert r.exit_code == 0, r.output
    assert fake_clone == {"url": "https://example.com/p.git", "sha": SHA}
    assert pip_calls and pip_calls[0][0] == "--no-deps"
    assert "requests>=2" in r.output and "NOT installed" in r.output


def test_git_install_is_refused_under_custody_before_the_clone(cli, fake_clone, monkeypatch):
    import core.packs.loader as loader
    monkeypatch.setattr(loader, "custody_refusal", lambda: "wallet custody: remedy")
    r = cli("install", f"git+https://example.com/p.git@{SHA}", "--accept-capabilities")
    assert r.exit_code != 0 and "wallet custody" in r.output
    assert fake_clone == {}


def test_path_install_refuses_a_first_party_claim_and_a_killed_pack(cli, pip_calls, tmp_path,
                                                                   monkeypatch):
    src = _third_party_source(tmp_path / "fp", tier="first-party")
    r = cli("install", str(src), "--accept-capabilities")
    assert r.exit_code != 0 and "first-party" in r.output
    src = _third_party_source(tmp_path / "tp")
    _kill(tmp_path, monkeypatch, _row("unrelated", dist="polyrob-thirdy"))
    r = cli("install", str(src), "--accept-capabilities")
    assert r.exit_code != 0 and "kill list" in r.output
    assert pip_calls == []


def test_path_install_refuses_a_core_or_index_distribution_name(cli, pip_calls, tmp_path):
    """A third-party source named like core or an index pack's dist would make
    pip REPLACE the reviewed code (and pass the loader's identity check)."""
    from core.packs.index import RETIRED_DISTS
    # 067 (one install): every retired separate pack name too (a squatter's
    # upload of it), in any spelling pip treats as the same distribution.
    names = ("polyrob", "Polyrob_X", *sorted(RETIRED_DISTS))
    for i, dist in enumerate(names):
        src = _third_party_source(tmp_path / f"s{i}")
        py = src / "pyproject.toml"
        py.write_text(py.read_text().replace('name = "polyrob-thirdy"', f'name = "{dist}"'))
        r = cli("install", str(src), "--accept-capabilities")
        assert r.exit_code != 0 and "cannot replace it" in r.output, (dist, r.output)
    assert pip_calls == []


def test_a_third_party_index_row_installs_the_hashed_wheel(cli, pip_calls, tmp_path, monkeypatch):
    rows = index.rows() + [{"id": "thirdy", "dist": "polyrob-thirdy", "version": "0.1.0",
                            "sha256": "c" * 64, "tier": "third-party", "summary": "",
                            "capabilities": ["tools"], "requires_core": "", "homepage": "",
                            "entry_point": "polyrob_thirdy:pack"}]
    path = tmp_path / "index.json"
    path.write_text(json.dumps(rows))
    monkeypatch.setattr(index, "INDEX_FILE", path)
    assert cli("install", "thirdy").exit_code != 0 and pip_calls == []
    r = cli("install", "thirdy", "--accept-capabilities")
    assert r.exit_code == 0, r.output
    assert pip_calls[0][:3] == ["--no-deps", "--require-hashes", "-r"]


# --- the one clone helper --------------------------------------------------------

def test_clone_audited_checks_out_exactly_the_pinned_commit(tmp_path):
    from cli.commands.skill_install import InstallError, clone_audited
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("one")
    subprocess.run(git + ["add", "a.txt"], check=True)
    subprocess.run(git + ["commit", "-qm", "one"], check=True)
    first = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                           check=True).stdout.strip()
    (repo / "a.txt").write_text("two")
    subprocess.run(git + ["commit", "-qam", "two"], check=True)
    sha = clone_audited(f"file://{repo}", tmp_path / "c1", sha=first)
    assert sha == first and (tmp_path / "c1" / "a.txt").read_text() == "one"
    with pytest.raises(InstallError):
        clone_audited(f"file://{repo}", tmp_path / "c2", sha="0" * 40)


