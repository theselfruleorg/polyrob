"""core/packs/retire.py — THE helper for the retired separate pack
distributions (067, one install): installer, deploy, update, migrate upgrade.

METADATA ONLY (Codex in-wheel re-check): a non-editable old pack wheel's RECORD
lists the ``polyrob_<id>/`` files the bundled polyrob wheel owns now, so
``pip uninstall`` would delete them. The helper removes the dist-info and the
top-level editable hooks only, then verifies one provider per pack."""
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from core.packs import retire as rt
from core.packs.index import RETIRED_DISTS

REPO = Path(__file__).resolve().parents[4]
OLD = sorted(RETIRED_DISTS)[-1]           # one retired name, from THE list
OLD_US = OLD.replace("-", "_")


def _old_dist(site: Path, *, editable: bool) -> metadata.Distribution:
    """A retired dist's metadata in *site*, as pip would have written it."""
    info = site / f"{OLD_US}-1.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {OLD}\nVersion: 1.1.0\n")
    (info / "entry_points.txt").write_text("[polyrob.packs]\nx = polyrob_x:pack\n")
    rows = [f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    if editable:
        for name in (f"__editable__.{OLD_US}-1.1.0.pth", f"__editable___{OLD_US}_1_1_0_finder.py"):
            (site / name).write_text("import x\n")
            rows.append(f"{name},,")
    else:
        pkg = site / "polyrob_x"
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").write_text("# owned by polyrob now\n")
        (pkg / "routes.py").write_text("# owned by polyrob now\n")
        rows += ["polyrob_x/__init__.py,,", "polyrob_x/routes.py,,"]
    # Hostile RECORD rows: never followed.
    outside = site.parent / "outside.pth"
    outside.write_text("keep\n")
    rows += ["../outside.pth,,", f"{outside},,", "sub/dir/x.pth,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return metadata.PathDistribution(info)


def test_a_non_editable_old_wheel_loses_only_its_metadata(tmp_path):
    site = tmp_path / "site-packages"
    dist = _old_dist(site, editable=False)
    res = rt.retire([dist], sites=[site])
    assert res.retired == [OLD] and not res.errors
    assert not (site / f"{OLD_US}-1.1.0.dist-info").exists()
    assert (site / "polyrob_x" / "__init__.py").is_file(), "never a pack file"
    assert (site / "polyrob_x" / "routes.py").is_file()
    assert (tmp_path / "outside.pth").is_file(), "never outside site-packages"


def test_an_editable_old_dist_loses_its_dist_info_pth_and_finder(tmp_path):
    site = tmp_path / "site-packages"
    dist = _old_dist(site, editable=True)
    res = rt.retire([dist], sites=[site])
    assert res.retired == [OLD] and not res.errors
    assert sorted(p.name for p in site.iterdir()) == []
    assert (tmp_path / "outside.pth").is_file()


def test_a_pth_pointing_at_an_old_pack_source_dir_goes(tmp_path):
    site = tmp_path / "site-packages"
    dist = _old_dist(site, editable=False)
    legacy = site / "legacy.pth"
    legacy.write_text(f"/opt/polyrob/packs/{OLD}\n")
    unrelated = site / "other.pth"
    unrelated.write_text("/opt/somewhere/else\n")
    record = site / f"{OLD_US}-1.1.0.dist-info" / "RECORD"
    record.write_text(record.read_text() + "legacy.pth,,\nother.pth,,\n")
    rt.retire([dist], sites=[site])
    assert not legacy.exists() and unrelated.exists()


def test_only_allowlisted_names_are_touched(tmp_path):
    info = tmp_path / "site" / "acme_pack-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-pack\nVersion: 1.0\n")
    (info / "RECORD").write_text("")
    res = rt.retire([metadata.PathDistribution(info)], sites=[info.parent])
    assert res.retired == [] and info.is_dir()


def test_the_helper_never_calls_pip():
    src = (REPO / "core/packs/retire.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"', "*", "``")))
    assert '"pip"' not in code and "'pip'" not in code


def _fake_probe(monkeypatch, eps):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"eps": eps, "missing": []}), "")
    monkeypatch.setattr(rt.subprocess, "run", fake_run)


ALL = {"discovery": [["polyrob", "polyrob_discovery:pack"]],
       "markets": [["polyrob", "polyrob_markets:pack"]], "x": [["polyrob", "polyrob_x:pack"]]}


def test_verify_names_a_double_provider(monkeypatch):
    _fake_probe(monkeypatch, {**ALL, "x": [["polyrob", "polyrob_x:pack"], [OLD, "polyrob_x:pack"]]})
    res = rt.verify(rt.Result())
    assert res.double_provided == ["x"] and not res.ok


def test_verify_fails_on_zero_providers(monkeypatch):
    """Exactly ONE provider (polyrob) per first-party pack: none is a failure."""
    _fake_probe(monkeypatch, {k: v for k, v in ALL.items() if k != "markets"})
    res = rt.verify(rt.Result())
    assert not res.ok and any("markets" in e for e in res.errors)


def test_verify_in_this_interpreter_is_clean():
    res = rt.verify(rt.Result())
    assert res.ok, (res.errors, res.double_provided)


def test_the_list_is_the_index_constant_and_the_script_is_stdlib_only():
    assert rt.retired_names() == RETIRED_DISTS
    src = (REPO / "core/packs/retire.py").read_text()
    assert "import core" not in src and "from core" not in src
    out = subprocess.run([sys.executable, str(REPO / "core/packs/retire.py")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_the_helper_ships_in_the_public_export():
    import re
    manifest = REPO / "scripts/public_manifest.txt"
    if not manifest.is_file():
        pytest.skip("scripts/ is not in the public export")
    pats = [l for l in manifest.read_text().splitlines() if l and not l.startswith("#")]
    assert any(re.search(p, "core/packs/retire.py") for p in pats)


# --- Codex final check: containment, own hooks only, the precondition ----------

def test_a_dist_info_outside_this_interpreters_site_dirs_is_skipped(tmp_path):
    site = tmp_path / "site-packages"
    dist = _old_dist(site, editable=True)
    res = rt.retire([dist], sites=[tmp_path / "elsewhere"])
    assert res.retired == [] and res.errors
    assert (site / f"{OLD_US}-1.1.0.dist-info").is_dir()


def test_a_symlinked_dist_info_is_skipped(tmp_path):
    real = tmp_path / "real"
    dist = _old_dist(real, editable=False)
    site = tmp_path / "site-packages"
    site.mkdir()
    link = site / f"{OLD_US}-1.1.0.dist-info"
    link.symlink_to(real / f"{OLD_US}-1.1.0.dist-info")
    res = rt.retire([metadata.PathDistribution(link)], sites=[site])
    assert res.retired == [] and (real / f"{OLD_US}-1.1.0.dist-info").is_dir()
    assert dist is not None


def test_a_dist_info_whose_directory_name_is_another_dist_is_skipped(tmp_path):
    """`acme-1.0.dist-info` claiming a retired Name: skipped with a warning."""
    site = tmp_path / "site-packages"
    info = site / "acme-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {OLD}\nVersion: 1.0\n")
    (info / "RECORD").write_text("")
    res = rt.retire([metadata.PathDistribution(info)], sites=[site])
    assert res.retired == [] and info.is_dir()
    assert any("acme" in e for e in res.errors)


def test_only_the_retired_dists_own_editable_hooks_go(tmp_path):
    site = tmp_path / "site-packages"
    dist = _old_dist(site, editable=True)
    core_hooks = [site / "__editable__.polyrob-1.2.0.pth", site / "__editable___polyrob_1_2_0_finder.py"]
    for h in core_hooks:
        h.write_text("x\n")
    record = site / f"{OLD_US}-1.1.0.dist-info" / "RECORD"
    record.write_text(record.read_text() + "".join(f"{h.name},,\n" for h in core_hooks))
    rt.retire([dist], sites=[site])
    assert all(h.is_file() for h in core_hooks), "never another dist's hook (polyrob's)"
    assert not (site / f"__editable__.{OLD_US}-1.1.0.pth").exists()


def test_nothing_is_deleted_unless_polyrob_already_provides_every_pack(tmp_path, monkeypatch, capsys):
    site = tmp_path / "site-packages"
    _old_dist(site, editable=True)
    _fake_probe(monkeypatch, {"x": [[OLD, "polyrob_x:pack"]]})
    monkeypatch.setattr(rt, "_sites", lambda: [site])
    monkeypatch.setattr(rt.metadata, "distributions",
                        lambda: [metadata.PathDistribution(site / f"{OLD_US}-1.1.0.dist-info")])
    assert rt.main([]) == 2
    assert (site / f"{OLD_US}-1.1.0.dist-info").is_dir()
    assert "reinstall polyrob first" in capsys.readouterr().err


def test_without_a_retired_dist_a_missing_provider_is_an_error_not_a_double(monkeypatch, capsys):
    _fake_probe(monkeypatch, {})
    monkeypatch.setattr(rt.metadata, "distributions", lambda: [])
    assert rt.main([]) == 1
    assert "reinstall polyrob first" in capsys.readouterr().err
