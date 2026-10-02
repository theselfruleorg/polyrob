"""The lock must serve EVERY Python the package declares, not the one that compiled it.

2026-09-22: `requirements.lock` pinned `numpy==2.5.3` (requires Python >=3.12) with
no marker while `pyproject.toml` floors at 3.11. `uv pip compile --universal` takes
the COMPILING interpreter as the lower bound unless `--python-version` names the
floor, so the lock was universal over 3.12+ only. Every 3.11 consumer of the lock
broke: the CI `prod-extras` job (`pip install -r requirements.txt` on 3.11),
`core/lazy_deps.py` installing numpy on a 3.11 box, `install.sh` extras. Prod
(3.12) never noticed. Two cheap offline checks keep the header honest.
"""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _floor() -> str:
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        spec = tomllib.load(fh)["project"]["requires-python"]
    m = re.search(r">=\s*(\d+\.\d+)", spec)
    assert m, f"requires-python has no >= floor: {spec!r}"
    return m.group(1)


def _lock_header() -> str:
    return "\n".join((_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()[:3])


def test_lock_was_compiled_for_the_pyproject_python_floor():
    floor = _floor()
    header = _lock_header()
    assert f"--python-version {floor}" in header, (
        f"regenerate with `python scripts/gen_lazy_closures.py --relock` (it runs "
        f"uv pip compile over pyproject.toml + packs/*/pyproject.toml with --all-extras "
        f"--universal --python-version {floor}); header was:\n{header}"
    )


def test_numpy_pin_is_marker_forked_or_floor_compatible():
    # numpy drops a Python minor roughly yearly; an unmarked pin is the one that
    # broke 3.11. Either it carries a python_full_version marker, or there is a
    # single pin (which the header test then guarantees resolves at the floor).
    lines = [l for l in (_ROOT / "requirements.lock").read_text().splitlines() if l.startswith("numpy==")]
    assert lines, "numpy is declared by the media and memory-vector extras"
    if len(lines) > 1:
        assert all("python_full_version" in l for l in lines), lines
