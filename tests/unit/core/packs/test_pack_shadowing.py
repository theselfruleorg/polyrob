"""An installed distribution cannot shadow a first-party pack (security
assessment 2026-09-25): neither by a second ``polyrob.packs`` entry point with
the same id, nor by claiming one of its tool ids first."""
from types import SimpleNamespace

from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _toml


def _ep(name, value, dist):
    return SimpleNamespace(name=name, value=value, module=value.partition(":")[0],
                           dist=SimpleNamespace(metadata={"Name": dist}, version="0.1.0"))


def _real_identity(scratch, monkeypatch):
    from core.packs.loader import _first_party_refusal
    monkeypatch.setattr(scratch, "_first_party_refusal", _first_party_refusal)
    monkeypatch.setattr(scratch, "_FIRST_PARTY", {"real": ("polyrob-real", "polyrob_real:pack")})


def test_a_second_entry_point_cannot_shadow_a_first_party_pack(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "real", _toml("real"))
    shadow = tmp_path / "aaa_shadow"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    (shadow / "pack.toml").write_text(_toml("real", tier="third-party"))
    _real_identity(scratch, monkeypatch)
    # "aaa_shadow:pack" sorts BEFORE "polyrob_real:pack".
    eps = [_ep("real", "polyrob_real:pack", "polyrob-real"),
           _ep("real", "aaa_shadow:pack", "evil-dist")]
    monkeypatch.setattr(scratch, "_entry_points", lambda: eps)
    scratch.register_policies()
    rec = state.record("real")
    assert rec.entry_point == "polyrob_real:pack", rec.entry_point
    assert rec.status == state.INSTALLED, rec.reason
    assert any("aaa_shadow:pack" in e for e in rec.errors)


def test_a_third_party_pack_cannot_squat_a_first_party_tool_id(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "real", _toml("real"))
    # "aaa" sorts before "real" and claims the first-party pack's tool id "realt".
    write_pack(tmp_path, monkeypatch, "aaa",
               _toml("aaa", tier="third-party").replace("aaat", "realt"))
    _real_identity(scratch, monkeypatch)
    eps = [_ep("aaa", "polyrob_aaa:pack", "polyrob-aaa"),
           _ep("real", "polyrob_real:pack", "polyrob-real")]
    monkeypatch.setattr(scratch, "_entry_points", lambda: eps)
    scratch.register_policies()
    assert state.record("real").status == state.INSTALLED, state.record("real").reason
    assert state.pack_of_tool("realt") == "real"
    assert state.record("aaa").status == state.REFUSED


# --- 067 (one install): the retired separate distributions ------------------------

from core.packs.index import RETIRED_DISTS  # noqa: E402 — THE list of retired names

_RETIRED = sorted(RETIRED_DISTS)


def _polyrob_identity(scratch, monkeypatch):
    from core.packs.loader import _first_party_refusal
    monkeypatch.setattr(scratch, "_first_party_refusal", _first_party_refusal)
    monkeypatch.setattr(scratch, "_FIRST_PARTY", {"real": ("polyrob", "polyrob_real:pack")})


def test_a_leftover_retired_dist_never_loads_twice_and_names_the_uninstall(
        scratch, tmp_path, monkeypatch):
    """The old editable pack dist left beside the new core: one record (the
    polyrob one), and a named error with the pip uninstall remedy."""
    write_pack(tmp_path, monkeypatch, "real", _toml("real"))
    _polyrob_identity(scratch, monkeypatch)
    eps = [_ep("real", "polyrob_real:pack", _RETIRED[-1]),    # the leftover
           _ep("real", "polyrob_real:pack", "polyrob")]
    monkeypatch.setattr(scratch, "_entry_points", lambda: eps)
    scratch.register_policies()
    recs = [r for r in state.records() if r.id == "real"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec.status == state.INSTALLED, rec.reason
    assert rec.dist_name == "polyrob"
    assert any(_RETIRED[-1] in e and "python -m core.packs.retire" in e for e in rec.errors), \
        rec.errors
    assert not any("pip uninstall" in e for e in rec.errors), "pip would delete pack files"


def test_a_retired_dist_alone_is_refused_with_the_remedy(scratch, tmp_path, monkeypatch):
    """A core whose metadata lacks the entry point (not reinstalled) plus the
    old dist — or a squatter's upload of the retired name — is refused, never
    loaded as first-party."""
    marker = tmp_path / "imported"
    write_pack(tmp_path, monkeypatch, "real", _toml("real"),
               f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    _polyrob_identity(scratch, monkeypatch)
    for name in (_RETIRED[1], _RETIRED[0].title().replace("-", "_")):
        state.reset()
        scratch._ORDER.clear()
        monkeypatch.setattr(scratch, "_entry_points",
                            lambda n=name: [_ep("real", "polyrob_real:pack", n)])
        scratch.load_packs()
        rec = state.record("real")
        assert rec.status == state.REFUSED
        assert "retired" in rec.reason and "python -m core.packs.retire" in rec.reason
    assert not marker.exists()
