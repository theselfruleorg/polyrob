"""066 P1 — the trusted lazy installer: a deploy-time install, deferred.

It installs ONE allowlisted feature from its shipped, hashed closure
(``core/lazy_closures/<feature>.txt``) into an overlay directory:

    <root>/<lock-digest>/<feature>/        (``.complete`` marks a finished install)

Two callers, one routine (:func:`install`):

* **prod** — ``polyrob-deps@<feature>.service`` / ``polyrob-deps@spool.service``
  run ``python -I -m core.lazy_installer --system --root /var/lib/polyrob-deps
  run <feature|spool>`` as the seedless ``polyrob-deps`` UID. The agent can read
  and import the overlay, never write it; its only write is a request file in
  ``<root>/requests/`` (the name, nothing else).
* **local / OSS** — ``core.lazy_deps.ensure`` calls :func:`install` in process,
  into ``~/.polyrob/pylibs``.

What makes a lazy install as trusted as a deploy (066 §4.1): the exact files of
the release lock (``--require-hashes``), no code run during install on prod
(``--only-binary :all:``), ``--no-deps`` (the closure IS the dependency set), an
isolated pip (``--isolated``: no pip.conf, no ``PIP_*`` env), a scrubbed child
env, an import probe in ``python -I`` before an atomic rename into place.

Refusals are NAMED (:class:`InstallRefused` ``.kind``): ``unknown_feature``,
``closure_missing``, ``sdist_refused``, ``no_wheel``, ``hash_mismatch``,
``pip_failed``, ``probe_failed``, ``wrong_identity``, ``secret_in_env``.

Layering: ``core`` only; imports nothing above it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The prod installer identity (deployment/hardening/install-deps-installer.sh).
DEPS_USER = "polyrob-deps"
#: The spool instance name: ``polyrob-deps@spool.service`` drains ``requests/``.
SPOOL_INSTANCE = "spool"
REQUESTS_DIR = "requests"
USED_FILE = "used.json"
COMPLETE = ".complete"
FAILED_SUFFIX = ".failed"
#: The only index a trusted install reads.
INDEX_URL = "https://pypi.org/simple"
#: Env names a seedless installer must never see (the wallet seeds, any key).
_SECRET_NAMES = ("AGENT_WALLET_MASTER_SEED", "PAYMENT_MASTER_SEED", "MASTER_SEED",
                 "EIP8004_AGENT_PRIVATE_KEY", "MCP_ENCRYPTION_KEY")

RunFn = Callable[..., subprocess.CompletedProcess]


class InstallRefused(RuntimeError):
    """A named refusal. ``kind`` is machine-readable; the message carries the remedy."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# --- names and paths ---------------------------------------------------------- #

def allowlist() -> Dict[str, tuple]:
    from core.lazy_deps import LAZY_DEPS
    return LAZY_DEPS


def valid_feature(name: str) -> bool:
    from core.lazy_closures import FEATURE_RE
    return isinstance(name, str) and bool(FEATURE_RE.match(name)) and name in allowlist()


def digest_dir(root: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", digest or ""):
        raise InstallRefused("closure_missing", f"invalid lock digest {digest!r}")
    return Path(root) / digest


def feature_dir(root: Path, digest: str, feature: str) -> Path:
    return digest_dir(root, digest) / feature


def is_complete(path: Path) -> bool:
    return (Path(path) / COMPLETE).is_file()


def failure_path(root: Path, digest: str, feature: str) -> Path:
    return digest_dir(root, digest) / f"{feature}{FAILED_SUFFIX}"


def read_failure(root: Path, digest: str, feature: str) -> Optional[dict]:
    try:
        return json.loads(failure_path(root, digest, feature).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- the environment the pip child sees --------------------------------------- #

def child_env() -> Dict[str, str]:
    """A minimal env: PATH/locale/proxy only. No ``PIP_*`` (``--isolated`` also
    ignores them), no key, no seed — ``build_child_env``'s rule, core-side."""
    from core.app_service.env_scan import SECRET_KEY_RE
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "HTTPS_PROXY", "https_proxy",
            "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE")
    env = {k: os.environ[k] for k in keep if k in os.environ and not SECRET_KEY_RE.search(k)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _installed_versions(python: str, run: RunFn) -> Dict[str, str]:
    """``{canonical name: version}`` of the base environment of *python* (no overlay)."""
    code = ("import importlib.metadata as m, json, re\n"
            "print(json.dumps({re.sub(r'[-_.]+','-',(d.metadata['Name'] or '')).lower(): d.version "
            "for d in m.distributions() if d.metadata['Name']}))")
    proc = run([python, "-I", "-c", code], capture_output=True, text=True, env=child_env())
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        return {}


def _classify(output: str, wheel_only: bool) -> tuple:
    text = output or ""
    if "DO NOT MATCH THE HASHES" in text or "hash mismatch" in text.lower():
        return "hash_mismatch", "a downloaded file does not match the sha256 recorded in the release lock"
    if wheel_only and ("No matching distribution" in text or "Could not find a version" in text):
        return "sdist_refused", ("no wheel for this platform, and this box installs wheels only "
                                 "(a source build would run a build backend)")
    if "No matching distribution" in text or "Could not find a version" in text:
        return "no_wheel", "no installable file for this platform in the lock"
    return "pip_failed", "pip failed"


# --- the install --------------------------------------------------------------- #

def install(feature: str, *, root: Path, wheel_only: bool, python: Optional[str] = None,
            run: Optional[RunFn] = None, index_args: Optional[List[str]] = None,
            record_used: bool = True) -> Path:
    """Install *feature* into ``<root>/<digest>/<feature>``; return that path.

    Idempotent: a finished install returns at once. Raises :class:`InstallRefused`.
    ``index_args`` replaces the PyPI index (tests use ``--no-index --find-links``).
    """
    from core.lazy_closures import read_closure
    python = python or sys.executable
    run = run or subprocess.run
    if not valid_feature(feature):
        raise InstallRefused("unknown_feature",
                             f"{feature!r} is not an allowlisted lazy feature; nothing installed")
    closure = read_closure(feature)
    if closure is None or not closure.lock_digest:
        raise InstallRefused("closure_missing",
                             f"{feature}: no shipped closure (core/lazy_closures/{feature}.txt); "
                             f"remedy: rerun scripts/gen_lazy_closures.py, or add the extra at deploy")
    if wheel_only and not closure.wheel:
        raise InstallRefused("sdist_refused",
                             f"{feature}: its closure has a package with no wheel, and this box "
                             f"installs wheels only; remedy: add the extra at deploy (PROD_EXTRAS)")
    root = Path(root)
    dest = feature_dir(root, closure.lock_digest, feature)
    if is_complete(dest):
        return dest
    ddir = digest_dir(root, closure.lock_digest)
    ddir.mkdir(parents=True, exist_ok=True)
    os.chmod(ddir, 0o755)

    have = _installed_versions(python, run)
    wanted = [(n, v, b) for n, v, b in closure.blocks() if have.get(n) != v]
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{feature}-", dir=ddir))
    try:
        if wanted:
            req = tmp / ".requirements.txt"
            req.write_text("".join(b for _, _, b in wanted), encoding="utf-8")
            target = tmp / "lib"
            cmd = [python, "-I", "-m", "pip", "--isolated", "install",
                   "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                   "--require-hashes", "--no-deps", "--no-compile",
                   "--target", str(target), "-r", str(req),
                   *(index_args if index_args is not None else ["--index-url", INDEX_URL])]
            cmd.append("--only-binary=:all:" if wheel_only else "--prefer-binary")
            logger.info("lazy install: %s (%d pin(s), wheel_only=%s)", feature, len(wanted), wheel_only)
            proc = run(cmd, capture_output=True, text=True, env=child_env())
            if proc.returncode != 0:
                out = ((proc.stderr or "") + (proc.stdout or "")).strip()
                kind, why = _classify(out, wheel_only)
                raise InstallRefused(kind, f"{feature}: {why}; nothing installed\n{out[-600:]}")
            req.unlink()
        else:
            (tmp / "lib").mkdir()
        # The probe: every distribution of the row is visible with the overlay
        # APPENDED to a clean interpreter's path — what the agent will see.
        names = [re.sub(r"[-_.]+", "-", _row_name(s)).lower() for s in allowlist()[feature]]
        probe = ("import sys, importlib.metadata as m\n"
                 f"sys.path.append({str(tmp / 'lib')!r})\n"
                 f"[m.distribution(n) for n in {names!r}]\n")
        proc = run([python, "-I", "-c", probe], capture_output=True, text=True, env=child_env())
        if proc.returncode != 0:
            raise InstallRefused("probe_failed",
                                 f"{feature}: installed files do not import cleanly; nothing installed\n"
                                 f"{(proc.stderr or '')[-400:]}")
        run([python, "-I", "-m", "compileall", "-q", str(tmp / "lib")],
            capture_output=True, text=True, env=child_env())
        (tmp / COMPLETE).write_text(json.dumps({
            "feature": feature, "lock_digest": closure.lock_digest,
            "requested": sorted(f"{n}=={v}" for n, v, _ in wanted),
            "wheel_only": wheel_only, "at": time.time()}), encoding="utf-8")
        _seal(tmp)
        try:
            os.rename(tmp, dest)
        except OSError:
            if not is_complete(dest):     # lost a race to a finished twin: fine
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        failure_path(root, closure.lock_digest, feature).unlink()
    except OSError:
        pass
    if record_used:
        _record_used(root, feature)
    logger.info("lazy install: %s ready at %s", feature, dest)
    return dest


def _row_name(spec: str) -> str:
    m = re.match(r"^[A-Za-z0-9._-]+", spec.strip())
    return m.group(0) if m else spec


def _seal(path: Path) -> None:
    """Read-only for everyone but the owner: dirs 0755, files 0644."""
    for dirpath, dirnames, filenames in os.walk(path):
        os.chmod(dirpath, 0o755)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, 0o755 if os.access(p, os.X_OK) else 0o644)


def _atomic_json(path: Path, value) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    with os.fdopen(fd, "w") as fh:
        json.dump(value, fh, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_used(root: Path) -> List[str]:
    try:
        data = json.loads((Path(root) / USED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(f for f in data.get("features", []) if valid_feature(f))


def _record_used(root: Path, feature: str) -> None:
    """``used.json``: the features the deploy pre-installs into the next overlay."""
    used = set(read_used(root)) | {feature}
    try:
        _atomic_json(Path(root) / USED_FILE, {"features": sorted(used)})
    except OSError as exc:
        logger.warning("lazy install: could not record %s in used.json: %s", feature, exc)


def _record_failure(root: Path, feature: str, exc: InstallRefused) -> None:
    from core.lazy_closures import read_closure
    closure = read_closure(feature)
    if closure is None or not closure.lock_digest:
        return
    try:
        target = failure_path(root, closure.lock_digest, feature)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o755)
        _atomic_json(target, {"kind": exc.kind, "reason": str(exc)[:800], "at": time.time()})
    except OSError:
        pass


# --- the spool (prod) ----------------------------------------------------------- #

def drain(root: Path, **kw) -> Dict[str, str]:
    """Process every request in ``<root>/requests/``: ``{name: outcome}``.

    A request is a FILE NAME. Its content is never read. A name that is not an
    allowlisted feature is deleted and refused (``bad_request``); nothing about
    it reaches pip or the filesystem outside ``requests/``.
    """
    spool = Path(root) / REQUESTS_DIR
    out: Dict[str, str] = {}
    try:
        names = sorted(os.listdir(spool))
    except OSError:
        return out
    for name in names:
        path = spool / name
        try:
            if path.is_symlink() or not path.is_file():
                raise InstallRefused("bad_request", "not a plain file")
            if not valid_feature(name):
                raise InstallRefused("bad_request", "not an allowlisted feature name")
        except InstallRefused as exc:
            logger.warning("lazy spool: refused request %r: %s", name[:80], exc)
            out[name] = f"refused:{exc.kind}"
            _unlink(path)
            continue
        try:
            install(name, root=root, **kw)
            out[name] = "installed"
        except InstallRefused as exc:
            logger.warning("lazy spool: %s refused (%s): %s", name, exc.kind, exc)
            _record_failure(root, name, exc)
            out[name] = f"refused:{exc.kind}"
        finally:
            _unlink(path)
    return out


def _unlink(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        pass


def prune(root: Path, keep_digest: str) -> List[str]:
    """Remove every overlay digest dir but *keep_digest* (after a verified deploy)."""
    removed = []
    for p in Path(root).iterdir():
        if p.is_dir() and re.fullmatch(r"[0-9a-f]{16}", p.name) and p.name != keep_digest:
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p.name)
    return removed


# --- identity checks for the system (prod) mode --------------------------------- #

def _whoami() -> str:
    import pwd
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def system_refusal() -> Optional[InstallRefused]:
    """The prod installer runs as ``polyrob-deps`` with no secret in its env.
    Anything else — the agent UID trying to write the overlay itself, a unit that
    grew an ``EnvironmentFile`` — refuses before touching the overlay."""
    me = _whoami()
    if me != DEPS_USER:
        return InstallRefused("wrong_identity",
                              f"agent write attempt refused: the overlay is written by {DEPS_USER} "
                              f"only (this process runs as {me}); request the feature instead")
    leaked = [n for n in _SECRET_NAMES if os.environ.get(n)]
    if leaked:
        return InstallRefused("secret_in_env",
                              f"the installer must never see a secret; {', '.join(leaked)} is set — "
                              f"remove the EnvironmentFile from polyrob-deps@.service")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="core.lazy_installer",
                                 description="066 P1 trusted lazy installer")
    ap.add_argument("--root", required=True, help="the overlay root, e.g. /var/lib/polyrob-deps")
    ap.add_argument("--system", action="store_true",
                    help="prod mode: wheel-only, must run as polyrob-deps with no secret in env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="install a feature, or drain the spool")
    r.add_argument("name", help=f"a lazy feature, or {SPOOL_INSTANCE!r}")
    sub.add_parser("used", help="print the features recorded in used.json")
    sub.add_parser("digest", help="print the shipped closures' lock digest")
    p = sub.add_parser("prune", help="remove every overlay but the current digest")
    p.add_argument("--keep")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from core.lazy_closures import shipped_lock_digest
    if args.cmd == "digest":
        print(shipped_lock_digest() or "")
        return 0
    if args.cmd == "used":
        print(" ".join(read_used(Path(args.root))))
        return 0
    if args.system:
        refusal = system_refusal()
        if refusal is not None:
            print(f"refused ({refusal.kind}): {refusal}", file=sys.stderr)
            return 3
    if args.cmd == "prune":
        keep = args.keep or shipped_lock_digest() or ""
        print(" ".join(prune(Path(args.root), keep)))
        return 0
    kw = {"wheel_only": bool(args.system)}
    if args.name == SPOOL_INSTANCE:
        results = drain(Path(args.root), **kw)
        for name, outcome in results.items():
            print(f"{name[:80]}: {outcome}")
        return 0
    try:
        path = install(args.name, root=Path(args.root), **kw)
    except InstallRefused as exc:
        if valid_feature(args.name):
            _record_failure(Path(args.root), args.name, exc)
        print(f"refused ({exc.kind}): {exc}", file=sys.stderr)
        return 2
    print(f"{args.name}: ready at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
