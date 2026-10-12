#!/usr/bin/env python3
"""066 P1 — regenerate the hashed install sets from ``requirements.lock``.

Writes:
  * ``core/lazy_closures/<feature>.txt`` — one per ``core.lazy_deps.LAZY_DEPS`` row;
  * ``requirements.txt`` — the server install set, for the extras on its
    ``# extras:`` line (must equal PROD_EXTRAS in scripts/deploy_prod.sh).

The first-party packs ship inside polyrob (067, one install): their SDKs are
core extras, so they need no closure file of their own.

Relock (compiles pyproject.toml into ONE lock, with the command
``core.lock_closure.lock_command`` builds), then regenerate:
    python devtools/gen_lazy_closures.py --relock
Only regenerate (after a hand-run relock):
    python devtools/gen_lazy_closures.py

``--check`` exits 1 when a file would change (what the drift test asserts).
``--check-wheels`` asks PyPI which closure pins have no linux x86_64 / cp312 wheel;
add each one it names to ``core.lock_closure.NO_WHEEL``.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import lock_closure as lc  # noqa: E402


def expected_files(root: Path = ROOT) -> dict:
    """``{path: text}`` for every generated file."""
    from core.lazy_deps import LAZY_DEPS, _dist_name
    lock_text = (root / "requirements.lock").read_text(encoding="utf-8")
    lock = lc.parse_lock(lock_text)
    out = {}
    for feature, specs in sorted(LAZY_DEPS.items()):
        out[root / "core" / "lazy_closures" / f"{feature}.txt"] = lc.render_feature(
            lock, feature, [_dist_name(s) for s in specs])
    req = root / "requirements.txt"
    text = req.read_text(encoding="utf-8")
    m = lc.REQUIREMENTS_TXT_EXTRAS_RE.search(text)
    if not m:
        raise SystemExit("requirements.txt has no `# extras: a,b,c` line")
    out[req] = lc.render_requirements_txt(lock_text, root / "pyproject.toml", m.group(1).split(","))
    return out


def relock(root: Path = ROOT) -> int:
    """Run THE relock command from *root*."""
    import subprocess
    cmd = lc.lock_command(root)
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=root).returncode


def _check_wheels(files: dict) -> int:
    missing = set()
    for path, text in files.items():
        if path.name == "requirements.txt":
            continue
        for line in text.splitlines():
            m = lc._REQ_RE.match(line)
            if not m:
                continue
            name, version = m.group(1), m.group(2)
            url = f"https://pypi.org/pypi/{name}/{version}/json"
            data = json.load(urllib.request.urlopen(url, timeout=30))
            ok = any(u["packagetype"] == "bdist_wheel" and (
                "none-any" in u["filename"] or
                ("x86_64" in u["filename"] and "linux" in u["filename"] and
                 any(t in u["filename"] for t in ("cp312", "abi3", "py3-none"))))
                for u in data["urls"])
            if not ok:
                missing.add(f"{lc.canon(name)}=={version}")
    for item in sorted(missing):
        print(f"no linux x86_64 cp312 wheel: {item}")
    return 1 if missing else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a generated file is stale")
    ap.add_argument("--check-wheels", action="store_true", help="ask PyPI for wheel coverage")
    ap.add_argument("--relock", action="store_true",
                    help="recompile requirements.lock with uv first")
    args = ap.parse_args(argv)
    if args.relock and relock() != 0:
        return 1
    files = expected_files()
    if args.check_wheels:
        return _check_wheels(files)
    closures = ROOT / "core" / "lazy_closures"
    stale = [p for p in closures.glob("*.txt") if p not in files]
    changed = [p for p, t in files.items() if not p.is_file() or p.read_text(encoding="utf-8") != t]
    if args.check:
        for p in changed + stale:
            print(f"stale: {p.relative_to(ROOT)}")
        return 1 if (changed or stale) else 0
    for p in stale:
        p.unlink()
        print(f"removed {p.relative_to(ROOT)}")
    for p in changed:
        p.write_text(files[p], encoding="utf-8")
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
