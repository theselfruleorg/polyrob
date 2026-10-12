"""W11 — pyproject extras are THE dependency spec; every runtime table agrees.

``core.lazy_deps.LAZY_DEPS`` is DERIVED (names from LAZY_FEATURES, version
clauses from the extra). The tables that cannot be derived at runtime — the
import-name maps in ``core.optional_extras`` name an extra whose module is
ABSENT — are pinned to ``pyproject.toml`` here.
"""
import tomllib
from pathlib import Path

import core.lazy_deps as ld
from core import extras_spec
from core.optional_extras import (EXTRA_FOR_MODULE, MODULES_FOR_EXTRA, dist_for_module)

_REPO = Path(__file__).resolve().parents[3]


def _extras() -> dict:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]
    return {k: {extras_spec.dist_name(s) for s in v}
            for k, v in project["optional-dependencies"].items()}


def test_extras_spec_reads_the_pyproject_extras():
    project = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]
    want = {k: tuple(v) for k, v in project["optional-dependencies"].items()
            if k not in extras_spec.META_EXTRAS}
    assert extras_spec.extra_specs() == want


def test_lazy_specs_are_the_pyproject_specs_verbatim():
    specs = extras_spec.extra_specs()
    for feature, row in ld.LAZY_DEPS.items():
        extra = ld._FEATURE_EXTRA[feature]
        for spec in row:
            assert spec in specs[extra], f"{feature}: {spec!r} is not a spec of [{extra}]"


def test_extra_feature_is_the_inverse_of_feature_extra():
    assert set(ld.EXTRA_FEATURE) == set(ld._FEATURE_EXTRA.values())
    for extra, feature in ld.EXTRA_FEATURE.items():
        assert ld._FEATURE_EXTRA[feature] == extra


def test_metadata_fallback_parses_requires_dist_markers(monkeypatch, tmp_path):
    class _Dist:
        requires = ['fastapi==1.0; extra == "server"', 'uvicorn[standard]==2; extra == "server"',
                    'polyrob[server]; extra == "all"', "pydantic>=2"]
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: _Dist())
    extras_spec._cached.cache_clear()
    try:
        assert extras_spec.extra_specs(tmp_path) == {"server": ("fastapi==1.0",
                                                                "uvicorn[standard]==2")}
    finally:
        extras_spec._cached.cache_clear()


def test_every_module_row_names_the_extra_that_declares_it():
    extras = _extras()
    for module, extra in EXTRA_FOR_MODULE.items():
        assert extra in extras, f"EXTRA_FOR_MODULE[{module!r}] = {extra!r}: no such extra"
        assert dist_for_module(module) in extras[extra], (
            f"EXTRA_FOR_MODULE[{module!r}] = {extra!r}, but [{extra}] does not declare "
            f"{dist_for_module(module)!r} (map the import in IMPORT_TO_DIST?)")


def test_every_probe_belongs_to_its_extra_and_every_extra_has_a_probe():
    extras = _extras()
    for extra, modules in MODULES_FOR_EXTRA.items():
        assert extra in extras, f"MODULES_FOR_EXTRA[{extra!r}]: no such extra"
        for module in modules:
            assert dist_for_module(module) in extras[extra], (extra, module)
            assert EXTRA_FOR_MODULE.get(module.split(".")[0]) in (extra, None) or \
                module.split(".")[0] == "numpy", (extra, module)
    # anysite is a console script, not an import (polyrob_discovery/anysite/client.py).
    unprobed = set(extras) - extras_spec.META_EXTRAS - set(MODULES_FOR_EXTRA) - {"anysite"}
    assert not unprobed, f"extras with no MODULES_FOR_EXTRA probe: {sorted(unprobed)}"


def test_every_extra_dist_with_an_import_has_a_module_row():
    """A missing module of any extra names its extra (EXTRA_FOR_MODULE)."""
    extras = _extras()
    known = {dist_for_module(m) for m in EXTRA_FOR_MODULE}
    # Helper dists never imported by name in our code (pulled by another row).
    helpers = {"anysite-cli", "httpx2", "httpcore2", "fastapi-x402",
               "py-clob-client-v2", "chatxdk"}
    gaps = {(extra, d) for extra, dists in extras.items()
            if extra not in extras_spec.META_EXTRAS for d in dists
            if d not in known and d not in helpers}
    assert not gaps, f"extra dists no EXTRA_FOR_MODULE row maps: {sorted(gaps)}"
