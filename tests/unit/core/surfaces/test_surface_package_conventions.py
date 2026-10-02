"""The conventions a new chat surface must follow — ENFORCED, not just written.

The factory builds surfaces autonomously (064 S2b W1+). Every rule below was
written down (AGENTS.md, surfaces/README.md, core/optional_extras.py) and none
of it failed a test (factory audit 2026-09-23):

1. a row's ``extra`` is a real pip extra, and a missing SDK gets the pip hint;
2. every pyproject extra that ships importable modules has a hint row;
3. the row's enable flag, owner env and credentials are documented flags
   (they are read through variables, so the literal-read reverse check never
   sees them);
4. a surface package imports WITHOUT its SDK — the SDK is imported lazily, so
   a bare install can list, probe and doctor every surface, and the gateway
   names the missing extra instead of crashing.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from core.surfaces import catalog

REPO = Path(__file__).resolve().parents[4]

#: Extras whose payload is not an importable module (a console script).
_NO_IMPORT_EXTRAS = {"all", "dev", "anysite"}


def _extras() -> dict:
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    return data["project"]["optional-dependencies"]


@pytest.mark.parametrize("spec", [s for s in catalog.SURFACES if s.extra], ids=lambda s: s.id)
def test_a_rows_extra_is_real_and_has_a_pip_hint(spec):
    from core.optional_extras import MODULES_FOR_EXTRA
    assert spec.extra in _extras(), f"{spec.id}: extra {spec.extra!r} not in pyproject"
    assert spec.extra in MODULES_FOR_EXTRA, (
        f"{spec.id}: add MODULES_FOR_EXTRA/EXTRA_FOR_MODULE rows for {spec.extra!r} in "
        f"core/optional_extras.py so a missing SDK names `pip install 'polyrob[{spec.extra}]'`")


def test_every_importable_extra_has_a_hint_row():
    from core.optional_extras import EXTRA_FOR_MODULE, MODULES_FOR_EXTRA
    missing = sorted(set(_extras()) - _NO_IMPORT_EXTRAS - set(MODULES_FOR_EXTRA))
    assert not missing, f"extras with no preflight/hint row in core/optional_extras.py: {missing}"
    for extra, modules in MODULES_FOR_EXTRA.items():
        for mod in modules:
            assert EXTRA_FOR_MODULE.get(mod.split(".")[0]) == extra or \
                mod.split(".")[0] in EXTRA_FOR_MODULE, (extra, mod)


@pytest.mark.parametrize("spec", catalog.SURFACES, ids=lambda s: s.id)
def test_a_rows_env_names_are_documented_flags(spec):
    from core.flags import REGISTRY, pattern_flag_for
    names = [spec.enabled_flag, *(spec.credentials)]
    if spec.owner_env:
        names.append(spec.owner_env)
    for alt in spec.alt_required:
        names.extend(alt)
    undocumented = [n for n in names if n not in REGISTRY and pattern_flag_for(n) is None]
    assert not undocumented, (
        f"{spec.id}: add docs/CONFIGURATION.md rows (+ both generators + the flag-count "
        f"ratchet) for {undocumented}")


def _blocked_modules() -> set:
    from core.optional_extras import MODULES_FOR_EXTRA
    mods = set()
    for spec in catalog.SURFACES:
        if spec.extra:
            mods.update(m.split(".")[0] for m in MODULES_FOR_EXTRA.get(spec.extra, ()))
    return mods


_PROBE = r"""
import importlib, sys
BLOCKED = set({blocked!r})

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
        return None

sys.meta_path.insert(0, _Block())
failed = []
for mod in {modules!r}:
    try:
        importlib.import_module(mod)
    except Exception as e:
        failed.append(f"{{mod}}: {{type(e).__name__}}: {{e}}")
print("\n".join(failed))
"""


def test_every_surface_package_imports_without_its_sdk():
    """A surface's SDK is imported inside the function that needs it. With every
    surface SDK blocked, each package's surface/harness/launch/probe must still
    import (the gateway then names the missing extra at launch)."""
    blocked = _blocked_modules()
    modules = [f"{s.module}.{part}" for s in catalog.SURFACES
               for part in ("surface", "harness", "launch", "probe")]
    code = _PROBE.format(blocked=sorted(blocked), modules=modules)
    res = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
    failed = [line for line in res.stdout.splitlines() if line.strip()]
    assert not failed, (
        "these surface modules import an SDK at module top — move the import into "
        "the function that needs it:\n  " + "\n  ".join(failed))
