"""A manifest cannot promote arbitrary installed code past the custody bar."""
from types import SimpleNamespace

from core.packs import state
from core.packs.loader import _first_party_refusal
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _install, _toml


def test_self_declared_first_party_never_imports_under_custody(scratch, tmp_path, monkeypatch):
    marker = tmp_path / "imported"
    write_pack(tmp_path, monkeypatch, "outsider", _toml("outsider"),
               f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    _install(scratch, monkeypatch, ["outsider"])
    monkeypatch.setattr(scratch, "_first_party_refusal", _first_party_refusal)
    monkeypatch.setenv("WALLET_SIGNER", "local")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    scratch.load_packs()
    rec = state.record("outsider")
    assert rec.status == state.REFUSED and "custody exemption" in rec.reason
    assert not marker.exists()


def test_known_pack_id_still_requires_its_distribution_and_entry_point():
    rec = SimpleNamespace(id="x", manifest=SimpleNamespace(tier="first-party"))
    def ep(dist, value):
        return SimpleNamespace(dist=SimpleNamespace(metadata={"Name": dist}), value=value)
    # 067 (one install): the identity is (dist polyrob, entry point).
    assert _first_party_refusal(rec, ep("outsider", "polyrob_x:pack"))
    assert _first_party_refusal(rec, ep("polyrob", "outsider:pack"))
    assert _first_party_refusal(rec, ep("polyrob", "polyrob_x:pack")) is None
    # The retired separate distribution name is not the identity any more.
    from core.packs.index import RETIRED_DISTS
    for name in RETIRED_DISTS:
        assert _first_party_refusal(rec, ep(name, "polyrob_x:pack"))


def test_the_index_identity_is_the_polyrob_distribution():
    from core.packs.index import first_party_identities
    ids = first_party_identities()
    assert ids == {"discovery": ("polyrob", "polyrob_discovery:pack"),
                   "markets": ("polyrob", "polyrob_markets:pack"),
                   "x": ("polyrob", "polyrob_x:pack")}


def test_a_shadowing_polyrob_metadata_without_pack_entry_points_is_named(monkeypatch):
    """2026-09-25 prod: /opt/polyrob/polyrob.egg-info (on PYTHONPATH) hid the
    dist-info's polyrob.packs group; the service loaded no pack, silently."""
    import importlib.metadata as md
    from core.packs import loader, state

    class _Stale:
        _path = "/opt/polyrob/polyrob.egg-info"
        entry_points = [md.EntryPoint("polyrob", "cli.polyrob:main", "console_scripts")]

    seen = []
    monkeypatch.setattr(md, "distribution", lambda name: _Stale())
    monkeypatch.setattr(state, "set_discovery_error", seen.append)
    loader._check_first_party_metadata()
    assert len(seen) == 1 and "polyrob.egg-info" in seen[0] and "discovery, markets, x" in seen[0]


def test_the_real_metadata_declares_every_first_party_pack(monkeypatch):
    from core.packs import loader, state
    seen = []
    monkeypatch.setattr(state, "set_discovery_error", seen.append)
    loader._check_first_party_metadata()
    assert seen == []
