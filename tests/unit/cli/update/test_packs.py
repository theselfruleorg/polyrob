"""067 — `polyrob update` and the first-party packs.

067 (one install): a tree that bundles its packs (polyrob.packs entry points in
its own pyproject) gets ONE install: the retired separate pack dists are
uninstalled first, and each carried pack arrives as its SDK extra. A rollback
target with the old separate-dist layout still installs its packs its own way
(closure with --packs, then each pack --no-deps, dependencies first); a pack the
tree lacks, or the kill list names, is skipped by name.
"""
from pathlib import Path

import pytest

from cli.update import packs as pk
from cli.update.detect import EDITABLE_GIT, InstallContext
from cli.update.runners import build_runners
from core.packs.index import RETIRED_DISTS

#: One retired separate pack distribution name, taken from THE list.
RETIRED_X = next(n for n in sorted(RETIRED_DISTS) if n.endswith("-x"))


def _pack(root: Path, dirname: str, pack_id: str, deps=()):
    """An OLD-layout pack: its own distribution (a rollback target's tree)."""
    d = root / "packs" / dirname
    d.mkdir(parents=True)
    dep_lines = ", ".join(f'"{x}"' for x in deps)
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "{dirname}"\nversion = "1.2.0"\ndependencies = [{dep_lines}]\n'
        f'[project.entry-points."polyrob.packs"]\n{pack_id} = "m:pack"\n')
    return d


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "requirements.lock").write_text("tweepy==4.17.0\n")
    _pack(tmp_path, "legacy-x", "x", ["polyrob==1.2.*", "tweepy"])
    _pack(tmp_path, "legacy-discovery", "discovery", ["polyrob==1.2.*"])
    return tmp_path


def _bundling_tree(root: Path) -> Path:
    """A 067 one-install tree: the packs are entry points of the core pyproject."""
    (root / "requirements.lock").write_text("tweepy==4.17.0\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "polyrob"\nversion = "1.2.0"\n'
        '[project.optional-dependencies]\ntwitter = ["tweepy"]\nanysite = ["anysite-cli"]\n'
        '[project.entry-points."polyrob.packs"]\n'
        'x = "polyrob_x:pack"\ndiscovery = "polyrob_discovery:pack"\n')
    for pid, extra in (("x", "twitter"), ("discovery", "anysite")):
        d = root / "packs" / pid / f"polyrob_{pid}"
        d.mkdir(parents=True)
        (d / "pack.toml").write_text(f'id = "{pid}"\nextra = "{extra}"\n')
    return root


def _installed(*rows):
    return [pk.InstalledPack(id=i, dist=d, version="1.1.0") for i, d in rows]


def test_carried_packs_are_the_retired_and_the_old_trees_first_party_ones(tree):
    have = _installed(("x", "legacy-x"), ("thirdparty", "acme-pack"),
                      ("discovery", "someone-elses-discovery"))
    # a third-party pack, and a same-id pack from another dist, are not carried
    assert pk.carried_packs(tree, have) == ["x"]
    assert pk.carried_packs(None, _installed(("markets", sorted(RETIRED_DISTS)[1]))) == ["markets"]


def _rec(tree, *, packs_listing=True):
    calls = []

    def run(cmd, cwd):
        calls.append(cmd)

    def capture(cmd, cwd):
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            return ""
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "main"
        if len(cmd) > 2 and cmd[1].endswith("lock_closure.py") and cmd[2] == "packs":
            if not packs_listing:
                raise RuntimeError("usage: lock_closure {deps,digest}")
            ids = cmd[cmd.index("--ids") + 1]
            rows = {"discovery": f"discovery legacy-discovery {tree}/packs/legacy-discovery",
                    "x": f"x legacy-x {tree}/packs/legacy-x"}
            want = rows if ids == "all" else {i: rows[i] for i in ids.split(",")}
            return "\n".join(want.values()) + "\n"
        return "OLDSHA123"
    return calls, run, capture


def test_apply_reinstalls_packs_after_core_through_the_hashed_closure(tree, monkeypatch):
    monkeypatch.setattr(pk, "kill_reason", lambda dist, version: None)
    calls, run, capture = _rec(tree)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture, extras=["docs"], packs=["x"])
    r.install()
    closure = next(c for c in calls if len(c) > 2 and c[1].endswith("lock_closure.py") and c[2] == "deps")
    assert closure[closure.index("--packs") + 1] == "x"
    project = ["/py", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."]
    pack = ["/py", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e",
            f"{tree}/packs/legacy-x"]
    hashed = next(c for c in calls if "--require-hashes" in c)
    assert calls.index(closure) < calls.index(hashed) < calls.index(project) < calls.index(pack)
    assert not any("legacy-discovery" in " ".join(c) for c in calls if c[:3] == ["/py", "-m", "pip"])


def test_a_killed_or_absent_pack_is_skipped_by_name(tree, monkeypatch, capsys):
    monkeypatch.setattr(pk, "kill_reason",
                        lambda dist, version: "on the pack kill list (x ==1.2.0)" if dist == "legacy-x" else None)
    calls, run, capture = _rec(tree)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture, extras=[], packs=["x", "gone"])
    r.install()
    err = capsys.readouterr().err
    assert "pack 'x' not installed: on the pack kill list" in err
    assert "pack 'gone' is not in this version's tree" in err
    assert not any("--packs" in c for c in calls)
    assert not any(str(c[-1]).endswith("legacy-x") for c in calls if c[:3] == ["/py", "-m", "pip"])


def test_a_tree_whose_resolver_predates_packs_still_updates_core(tree, monkeypatch, capsys):
    """The rollback target may be a tree without the `packs` subcommand: core
    reinstalls, no --packs flag is passed, the editable packs stay as they are."""
    monkeypatch.setattr(pk, "kill_reason", lambda dist, version: None)
    calls, run, capture = _rec(tree, packs_listing=False)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture, extras=[], packs=["x"])
    r.install()
    assert "cannot list its packs" in capsys.readouterr().err
    assert ["/py", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."] in calls
    assert not any("--packs" in c for c in calls)


def test_kill_reason_consults_the_one_kill_list_when_core_has_it(monkeypatch):
    import sys
    import types
    fake = types.ModuleType("core.packs.index")
    fake.killed = lambda name, version: f"killed {name} {version}"
    monkeypatch.setitem(sys.modules, "core.packs.index", fake)
    assert pk.kill_reason("legacy-x", "1.2.0") == "killed legacy-x 1.2.0"
    monkeypatch.setitem(sys.modules, "core.packs.index", None)  # a core without P7
    assert pk.kill_reason("legacy-x", "1.2.0") is None


def _kill_list(monkeypatch, tmp_path, rows):
    import json
    import core.packs.index as idx
    f = tmp_path / "removed.json"
    f.write_text(rows if isinstance(rows, str) else json.dumps(rows))
    monkeypatch.setattr(idx, "REMOVED_FILE", f)


def test_update_refuses_a_pack_on_the_real_kill_list_by_name(tree, tmp_path, monkeypatch, capsys):
    """P7's core.packs.index.killed is THE kill list: an installed pack whose
    new version it names is not reinstalled, and the reason is printed."""
    _kill_list(monkeypatch, tmp_path, [{"id": "x", "dist": "legacy-x", "versions": "==1.2.0",
                                        "reason": "credential exfiltration", "date": "2026-09-24"}])
    assert "credential exfiltration" in pk.kill_reason("legacy-x", "1.2.0")
    assert pk.kill_reason("legacy-x", "1.1.0") is None
    calls, run, capture = _rec(tree)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture, extras=[],
                      packs=["discovery", "x"])
    r.install()
    err = capsys.readouterr().err
    assert "pack 'x' not installed: on the pack kill list" in err and "credential exfiltration" in err
    pip = [" ".join(c) for c in calls if c[:3] == ["/py", "-m", "pip"]]
    assert any(p.endswith("packs/legacy-discovery") for p in pip)
    assert not any(p.endswith("packs/legacy-x") for p in pip)
    deps = next(c for c in calls if len(c) > 2 and c[1].endswith("lock_closure.py") and c[2] == "deps")
    assert deps[deps.index("--packs") + 1] == "discovery"


def test_an_unreadable_kill_list_refuses_every_pack(tree, tmp_path, monkeypatch, capsys):
    """A kill list that cannot be read is not an empty one: nothing is reinstalled."""
    _kill_list(monkeypatch, tmp_path, "{not json")
    calls, run, capture = _rec(tree)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture, extras=[], packs=["x"])
    r.install()
    assert "kill list is unreadable" in capsys.readouterr().err
    assert not any("--packs" in c for c in calls)


def test_an_id_only_kill_row_refuses_the_pack(tree, tmp_path, monkeypatch, capsys):
    """Codex 067 follow-up #2: a kill row needs only an ``id``; the updater
    asked by dist alone, so an id-only revocation let the pack install."""
    _kill_list(monkeypatch, tmp_path, [{"id": "x", "versions": "==1.2.0",
                                        "reason": "revoked", "date": "2026-09-25"}])
    calls, run, capture = _rec(tree)
    ctx = InstallContext(EDITABLE_GIT, tree, tree, "editable")
    build_runners(ctx, python="/py", run=run, capture=capture, extras=[],
                  packs=["discovery", "x"]).install()
    assert "pack 'x' not installed: on the pack kill list" in capsys.readouterr().err
    pip = [" ".join(c) for c in calls if c[:3] == ["/py", "-m", "pip"]]
    assert not any(p.endswith("packs/legacy-x") for p in pip)
    assert any(p.endswith("packs/legacy-discovery") for p in pip)


def test_a_killed_dependency_in_the_resolved_set_is_refused(tree, tmp_path, monkeypatch):
    """Every pack of the RESOLVED set is checked, not only the wanted ones: a
    killed dependency is not installed, nor is the pack that needs it."""
    _kill_list(monkeypatch, tmp_path, [{"id": "discovery", "versions": ">=0",
                                        "reason": "revoked", "date": "2026-09-25"}])
    d = f"discovery legacy-discovery {tree}/packs/legacy-discovery"
    x = f"x legacy-x {tree}/packs/legacy-x"

    def capture(cmd, cwd):
        ids = cmd[cmd.index("--ids") + 1]
        return {"all": f"{d}\n{x}\n", "x": f"{d}\n{x}\n", "discovery": f"{d}\n"}[ids]

    notes = []
    assert pk.resolve_install("/py", tree, ["x"], capture, notes) == []
    assert any("'discovery' not installed" in n for n in notes)
    assert any("'x' not installed: it depends on killed pack(s) discovery" in n for n in notes)


# --- 067 (one install): a tree that bundles its packs ---------------------------

def test_update_to_a_bundling_tree_retires_the_old_dists_after_core(tmp_path, monkeypatch):
    """The retired editable pack dist is uninstalled BEFORE the project install
    (one provider of the entry points after it), and its pack is carried as
    the SDK extra — no pack directory is installed."""
    repo = _bundling_tree(tmp_path)
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("x", RETIRED_X),
                                                                  ("acme", "acme-pack")))
    calls, run, capture = _rec(repo)
    ctx = InstallContext(EDITABLE_GIT, repo, repo, "editable")
    build_runners(ctx, python="/py", run=run, capture=capture, extras=["docs"]).install()
    retire = ["/py", str(repo / "core" / "packs" / "retire.py")]
    project = ["/py", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."]
    assert calls.index(project) < calls.index(retire), "metadata-only retire AFTER the project"
    assert not any("uninstall" in c for c in calls), "pip uninstall deletes pack files"
    deps = next(c for c in calls if len(c) > 2 and c[1].endswith("lock_closure.py") and c[2] == "deps")
    assert deps[deps.index("--extras") + 1] == "docs,twitter", "the pack arrives as its extra"
    assert "--packs" not in deps
    pip = [c for c in calls if c[:3] == ["/py", "-m", "pip"]]
    assert not any("acme-pack" in " ".join(c) for c in pip), "a third-party pack is untouched"
    assert not any("/packs/" in " ".join(c) for c in pip)
    assert not any(c[2] == "packs" for c in calls if len(c) > 2 and c[1].endswith("lock_closure.py"))


def test_update_to_a_bundling_tree_without_leftovers_uninstalls_nothing(tmp_path, monkeypatch):
    repo = _bundling_tree(tmp_path)
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("x", "polyrob")))
    calls, run, capture = _rec(repo)
    build_runners(InstallContext(EDITABLE_GIT, repo, repo, "editable"), python="/py", run=run,
                  capture=capture, extras=["twitter"]).install()
    assert not any("uninstall" in c for c in calls)
    assert not any(str(c[-1]).endswith("retire.py") for c in calls)


def test_bundled_extras_read_the_trees_pack_manifests(tmp_path):
    repo = _bundling_tree(tmp_path)
    assert pk.tree_bundles_packs(repo) and not pk.tree_bundles_packs(tmp_path / "nope")
    assert pk.bundled_extras(repo, ["x", "discovery", "markets"]) == ["anysite", "twitter"]


def test_the_real_tree_bundles_its_packs():
    root = Path(__file__).resolve().parents[4]
    assert pk.tree_bundles_packs(root) and pk.tree_packs(root) == []
    assert pk.bundled_extras(root, ["x", "discovery", "markets"]) == ["anysite", "crypto", "twitter"]


def test_manual_steps_retire_the_old_dist_and_carry_the_extra(tree, monkeypatch):
    from cli.commands import update as upd
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("x", RETIRED_X)))
    monkeypatch.setattr("cli.update.extras.installed_extras", lambda repo: [])
    steps = upd._manual_steps_for(EDITABLE_GIT, tree)
    assert "pip install --no-deps --no-build-isolation -e . && python core/packs/retire.py && " in steps
    assert "uninstall" not in steps
    assert "--packs x -o polyrob-deps.txt" in steps
    assert "lock_closure.py packs" not in steps and '"$d"' not in steps
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("x", "polyrob")))
    assert "retire.py" not in upd._manual_steps_for(EDITABLE_GIT, tree)


def test_wheel_advice_never_upgrades_a_pack_from_the_index(monkeypatch):
    """Codex 067 follow-up #1: a wheel install's advice must not append pack
    dists to an unrestricted `pip install -U`. 067 (one install): a first-party
    pack ships inside polyrob (no note), a retired dist is uninstalled, a
    third-party pack gets a pinned re-install and is never auto-upgraded."""
    import shlex
    from cli.commands import update as upd
    from cli.update.detect import PIP
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(
        ("markets", "polyrob"), ("x", RETIRED_X), ("acme", "acme-private-pack"),
        ("discovery", "evil-discovery")))  # same id as a first-party pack, other dist
    monkeypatch.setattr("cli.update.extras.installed_extras", lambda repo: [])
    steps = upd._manual_steps_for(PIP, None)
    cmd = steps.split("  ", 1)[0]
    assert shlex.split(cmd) == ["python", "-m", "pip", "install", "-U", "polyrob"]
    assert f"{RETIRED_X}: the packs ship inside polyrob now — after the upgrade run " \
           "python -m core.packs.retire" in steps
    assert "pip uninstall" not in steps.replace("never pip uninstall", "")
    assert "third-party pack acme (acme-private-pack): never auto-upgraded" in steps
    assert "third-party pack discovery (evil-discovery)" in steps
    assert "pack markets" not in steps and "install.sh" not in steps
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("markets", "polyrob")))
    assert "packs are not upgraded" not in upd._manual_steps_for(PIP, None)


def test_the_update_status_names_a_leftover_retired_dist(monkeypatch):
    monkeypatch.setattr(pk, "installed_packs", lambda: _installed(("x", RETIRED_X)))
    (line,) = pk.retired_note()
    assert RETIRED_X in line and "python core/packs/retire.py" in line
    monkeypatch.setattr(pk, "installed_packs", lambda: [])
    assert pk.retired_note() == []
