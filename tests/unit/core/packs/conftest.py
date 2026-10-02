"""Fixtures for the pack loader tests (067 P2).

``scratch`` gives each test empty-of-packs registries: copies of the verb and
capability tables with NO built views recorded (so a test can play phase 1),
a copy of the live-gate store, a fresh loader state, and no pack env flags.
``use_packs(*names)`` makes the loader see fixture packs as installed entry
points (``tests/fixtures/packs/<name>`` on ``sys.path``).
"""
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "packs"


@pytest.fixture
def scratch(monkeypatch):
    from core import tool_capabilities as tc
    from core import tool_gates, verb_policy as vp
    from core.packs import loader, state
    import tools.descriptors as desc

    # Synthetic first-party fixtures stand in for reviewed distribution metadata.
    # The trust-boundary tests explicitly restore the real predicate.
    monkeypatch.setattr(loader, "_first_party_refusal", lambda rec, ep: None)

    monkeypatch.setattr(vp, "_REGISTRY", dict(vp._REGISTRY))
    monkeypatch.setattr(vp, "_SOURCES", dict(vp._SOURCES))
    monkeypatch.setattr(vp, "_BUILT_VIEWS", {})
    monkeypatch.setattr(tc, "TOOL_CAPABILITIES", dict(tc.TOOL_CAPABILITIES))
    monkeypatch.setattr(tc, "TOOL_PERMISSIONS", dict(tc.TOOL_PERMISSIONS))
    monkeypatch.setattr(tc, "_BUILT_VIEWS", {})
    monkeypatch.setattr(tc, "_SOURCES", {})
    monkeypatch.setattr(tool_gates, "_GATES", dict(tool_gates._GATES))
    # 067 P3b: pack surface rows and cron delivery channels.
    from core import delivery_channels
    from core.surfaces import catalog
    monkeypatch.setattr(catalog, "_PACK_SURFACES", [])
    monkeypatch.setattr(delivery_channels, "_CHANNELS", {})
    for name in ("POLYROB_PACKS", "POLYROB_PACKS_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    descriptors = dict(desc.TOOL_DESCRIPTORS)
    components = list(desc.TOOL_COMPONENTS)
    # The session's real loader state (an installed pack, e.g. discovery, loaded
    # by tests/conftest.py) is put back afterwards, and only the pack modules a
    # test imported are dropped — the installed pack's modules stay the ones
    # its tests hold references to.
    saved = (dict(state._RECORDS), dict(state._TOOL_OWNER), dict(state._PHASES),
             list(state._DISCOVERY_ERROR), list(loader._ORDER))
    modules_before = {m for m in sys.modules if m.startswith("polyrob_")}
    loader.reset_for_tests()
    yield loader
    loader.reset_for_tests()
    state._RECORDS.update(saved[0])
    state._TOOL_OWNER.update(saved[1])
    state._PHASES.update(saved[2])
    state._DISCOVERY_ERROR[:] = saved[3]
    loader._ORDER[:] = saved[4]
    desc.TOOL_DESCRIPTORS.clear()
    desc.TOOL_DESCRIPTORS.update(descriptors)
    desc.TOOL_COMPONENTS[:] = components
    for mod in [m for m in sys.modules if m.startswith("polyrob_") and m not in modules_before]:
        del sys.modules[mod]


@pytest.fixture
def use_packs(monkeypatch, scratch):
    def _use(*specs):
        eps = []
        for spec in specs:
            name, _, value = spec.partition("=")
            value = value or f"polyrob_{name}:pack"
            root = FIXTURES / name
            if root.is_dir():
                monkeypatch.syspath_prepend(str(root))
            eps.append(EntryPoint(name=name, value=value, group="polyrob.packs"))
        monkeypatch.setattr(scratch, "_entry_points", lambda: eps)
        return scratch
    return _use


def write_pack(tmp_path, monkeypatch, pack_id, toml, init=None):
    """A throwaway pack package ``polyrob_<id>`` under *tmp_path* (on sys.path)."""
    pkg = tmp_path / f"polyrob_{pack_id}"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "pack.toml").write_text(toml)
    (pkg / "__init__.py").write_text(init or (
        "from core.packs.spec import PackSpec\n"
        f"def pack():\n    return PackSpec(id={pack_id!r})\n"))
    monkeypatch.syspath_prepend(str(tmp_path))
    return pkg
