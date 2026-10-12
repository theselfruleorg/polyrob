"""On-demand optional dependencies (058 WS-7; trusted since 066 P1).

An optional capability names its extra; the FIRST USE of that capability installs
it. This is what makes the lean base install free rather than a trade-off: a
Gemini user sets a key and the SDK arrives, instead of reading an error and
learning what an extra is.

066 P1: a lazy install is a DEPLOY-TIME INSTALL, DEFERRED — the exact files of the
release lock, verified the same way (``LAZY_DEPS_MODE``, default ``trusted``):

* the closure is shipped and hashed (``core/lazy_closures/<feature>.txt``,
  generated from ``requirements.lock`` by ``devtools/gen_lazy_closures.py``);
* it lands in an OVERLAY (``<root>/<lock-digest>/<feature>/``) appended to the END
  of ``sys.path`` — the base venv always wins a name collision, and the venv the
  deploy verified is never mutated;
* on a provisioned server (``<data home>-deps/requests/`` exists) the agent only
  WRITES A REQUEST; the seedless ``polyrob-deps`` unit installs, wheel-only, into
  an overlay the agent can read but not write (:mod:`core.lazy_installer`);
* locally it installs in process into ``~/.polyrob/pylibs``, hashed, in a
  scrubbed child env; under wallet custody wheel-only; elsewhere a source build is
  allowed where a platform has no wheel (066 D2).

``legacy`` installs the shipped hashed wheel closure into THIS venv, and
still refuses under custody (CR-M09). ``off`` refuses and names the extra.

Ported from NousResearch/hermes-agent's tools/lazy_deps.py (@6bde794) with its
security model intact — allowlist, per-spec safety re-check, consent gate, honest
refusal.

Layering: this module lives in ``core/`` and imports nothing above it.
"""
from __future__ import annotations

import importlib
import importlib.metadata as _md
import logging
import os
import stat
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: feature -> (the pyproject extra it belongs to, the distributions of that
#: extra it installs). ⚠️ THE allowlist: a distribution that is not named here
#: never reaches pip. W11: the VERSION CLAUSES are not copied here — they come
#: from that extra in ``pyproject.toml`` (THE dependency spec, read through
#: ``core.extras_spec``), and the exact version is decided by the lock (see
#: ``_constraint_args``). Row order matters once: the FIRST feature of an extra
#: is the one ``EXTRA_FEATURE`` installs for the whole extra.
LAZY_FEATURES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "provider.gemini": ("gemini", ("google-generativeai",)),
    "provider.anthropic": ("anthropic", ("anthropic",)),
    "docs.pdf": ("docs", ("pypdf",)),
    "docs.docx": ("docs", ("python-docx",)),
    "memory.vector": ("memory-vector", ("apsw", "sqlite-vec", "numpy")),
    "media.qr": ("media", ("qrcode",)),
    "media.gif": ("media", ("imageio",)),
    "tool.anysite": ("anysite", ("anysite-cli",)),
    # 062: the three extras a first run actually asks for. `polyrob serve`,
    # `polyrob telegram` and voice transcription each used to stop at "install
    # the extra yourself" on the machine where lazy installs are ON — the one
    # machine where we can just do it. Every name is still on THE allowlist;
    # `browser` is deliberately absent, because a first-use pip install that
    # then downloads ~150 MB of Chromium mid-turn is a hang, not a capability.
    "server.api": ("server", ("fastapi", "starlette", "uvicorn", "jinja2",
                              "python-socketio", "python-multipart", "watchfiles",
                              "argon2-cffi", "python-magic")),
    "surface.telegram": ("telegram", ("aiogram",)),
    "voice.transcribe": ("voice", ("faster-whisper",)),
}


def _derive_lazy_deps() -> Dict[str, Tuple[str, ...]]:
    """feature -> the PyPI specs that satisfy it: each allowlisted name with the
    version clause its extra declares (a bare name when the spec is unreadable)."""
    from core.extras_spec import dist_name, extra_specs
    try:
        extras = extra_specs()
    except Exception:  # noqa: BLE001 — bare names are still the allowlist
        extras = {}
    out: Dict[str, Tuple[str, ...]] = {}
    for feature, (extra, names) in LAZY_FEATURES.items():
        declared = {dist_name(spec): spec for spec in extras.get(extra, ())}
        out[feature] = tuple(declared.get(dist_name(n), n) for n in names)
    return out


#: feature -> the PyPI specs that satisfy it (derived; see LAZY_FEATURES).
LAZY_DEPS: Dict[str, Tuple[str, ...]] = _derive_lazy_deps()

#: feature -> the pyproject extra a server operator must declare (PROD_EXTRAS).
_FEATURE_EXTRA: Dict[str, str] = {f: extra for f, (extra, _) in LAZY_FEATURES.items()}

#: pyproject extra -> the lazy feature that installs it, for the ONE preflight
#: (`cli.commands._errors.require_extra_or_exit`). An extra with no row here is
#: reported, never installed. Derived: the first LAZY_FEATURES row of the extra.
EXTRA_FEATURE: Dict[str, str] = {}
for _feature, (_extra, _) in LAZY_FEATURES.items():
    EXTRA_FEATURE.setdefault(_extra, _feature)
del _feature, _extra


def feature_for_extra(extra: str) -> Optional[str]:
    """The lazy feature that installs *extra*, or None when there is none."""
    return EXTRA_FEATURE.get(str(extra).strip().lower())

#: Deps of a lazy-managed extra that deliberately have NO lazy row: the vector
#: backend degrades to keyword recall without an embedder, so the embedder is
#: an operator's choice, never something a first use pulls in (it is ~2 GB with
#: torch). Read by the pyproject drift test.
_EXTRA_DEPS_WITHOUT_LAZY_ROW = frozenset({"sentence-transformers"})

#: A PyPI name, an optional extras bracket, an optional version clause. Nothing
#: else: no URL, no path, no VCS ref, no marker, no option, no hash, no space.
_SAFE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:\s*(?:[<>=!~]=?|===)\s*[A-Za-z0-9.*+!-]+(?:\s*,\s*(?:[<>=!~]=?|===)\s*[A-Za-z0-9.*+!-]+)*)?$"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+")


class FeatureUnavailable(RuntimeError):
    """An optional capability is absent and could not (or may not) be installed.
    The message always carries the remedy; ``kind`` names the refusal
    (``unknown_feature``, ``off``, ``hash_mismatch``, ``sdist_refused``, …)."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


# --- policy ----------------------------------------------------------------- #

def lazy_mode() -> str:
    """``LAZY_DEPS_MODE``: ``off`` | ``trusted`` (default) | ``legacy``."""
    from core.config_policy.capability_toggles import lazy_deps_mode
    return lazy_deps_mode()


def lazy_installs_enabled() -> bool:
    """Any mode but ``off``."""
    return lazy_mode() != "off"


def remedy(feature: str) -> str:
    """The one sentence every refusal carries: the pip extra, and on a server the
    deploy-side list it belongs in."""
    extra = _FEATURE_EXTRA.get(feature, feature)
    text = f"pip install 'polyrob[{extra}]'"
    if not lazy_installs_enabled():
        text += (f" (on a deployed server add `{extra}` to the extras your deploy "
                 f"installs and redeploy, or set LAZY_DEPS_MODE=trusted; "
                 f"lazy installs are off here)")
    return text


# --- the overlay ------------------------------------------------------------ #

#: How long ensure() waits for the installer unit (seconds).
SPOOL_WAIT_SEC = 120.0
_POLL_SEC = 0.25


def system_root() -> Path:
    """Trusted sibling of the shared data home (normally /var/lib/polyrob-deps).

    A shared data-home parent can replace a read-only child, so executable
    overlays cannot live underneath that parent. Local installs use local_root.
    """
    from core.runtime_paths import effective_data_home
    data_home = effective_data_home()
    return data_home.with_name(data_home.name + "-deps")


def local_root() -> Path:
    """The local/OSS overlay root: ``~/.polyrob/pylibs`` (``POLYROB_HOME`` aware)."""
    from core.paths import polyrob_home
    return polyrob_home() / "pylibs"


def spool_provisioned() -> bool:
    """True when the installer unit is set up here (its request dir exists)."""
    try:
        return (system_root() / "requests").is_dir()
    except Exception:
        return False


def _lock_digest() -> Optional[str]:
    from core.lazy_closures import shipped_lock_digest
    try:
        return shipped_lock_digest()
    except Exception:
        return None


def overlay_paths() -> List[Path]:
    """Every finished feature dir of the CURRENT lock digest, both roots."""
    digest = _lock_digest()
    if not digest:
        return []
    out: List[Path] = []
    protected = spool_provisioned() or (_custody() and not _local_mode())
    roots = []
    for root in ((system_root(),) if protected else (system_root(), local_root())):
        if root not in roots:
            roots.append(root)
    for root in roots:
        base = root / digest
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child / ".complete").is_file() and (child / "lib").is_dir():
                if not _protected_overlay(child, local=not protected):
                    logger.warning("refusing untrusted lazy overlay: %s", child)
                    continue
                out.append(child / "lib")
    return out


def _protected_overlay(feature: Path, *, local: bool = False) -> bool:
    """Validate ownership/modes independently of the caller's privileges.

    Root runs deployment import checks too: os.access(W_OK) would reject every
    overlay for root, while a permission-bit check models agent access. Named
    writable POSIX ACLs also require a writable group mask and fail this check.
    """
    import pwd
    from core.lazy_installer import DEPS_USER, valid_feature
    try:
        trusted = {0, os.geteuid()} if local else {0, pwd.getpwnam(DEPS_USER).pw_uid}
        if not valid_feature(feature.name):
            return False
        child = feature.absolute()
        for path in (child, *child.parents):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in trusted:
                return False
            if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                # A root-owned sticky parent prevents other identities from
                # replacing its trusted child while allowing shared state.
                if path == child or not (info.st_mode & stat.S_ISVTX) or info.st_uid != 0:
                    return False
                if child.lstat().st_uid not in trusted:
                    return False
            child = path
        for directory, directories, files in os.walk(feature, followlinks=False):
            for path in [Path(directory), *(Path(directory) / n for n in directories + files)]:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or info.st_uid not in trusted \
                        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return False
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    return False
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    return False
        return True
    except (OSError, KeyError):
        return False

def activate_overlay() -> List[str]:
    """Append the finished overlays to the END of ``sys.path`` (idempotent).
    The base venv keeps precedence on every name. Returns the paths added."""
    added = []
    for p in overlay_paths():
        s = str(p)
        if s not in sys.path:
            sys.path.append(s)
            added.append(s)
    if added:
        importlib.invalidate_caches()
    return added


def overlay_status() -> Dict[str, object]:
    """For doctor/status: the digest, the mode, and what is installed where."""
    digest = _lock_digest()
    return {
        "mode": lazy_mode(),
        "lock_digest": digest,
        "spool": spool_provisioned(),
        "system_root": str(system_root()),
        "local_root": str(local_root()),
        "installed": sorted(p.parent.name for p in overlay_paths()),
    }


# --- read side -------------------------------------------------------------- #

def _dist_name(spec: str) -> str:
    """``anthropic>=0.20.0`` -> ``anthropic``; ``uvicorn[standard]==1`` -> ``uvicorn``."""
    m = _NAME_RE.match(spec.strip())
    return (m.group(0) if m else spec).lower()


def _dist_present(name: str) -> bool:
    try:
        _md.distribution(name)
        return True
    except _md.PackageNotFoundError:
        return False


def is_available(feature: str) -> bool:
    """No install, no side effect: are all of the feature's distributions present?"""
    specs = LAZY_DEPS.get(feature)
    if not specs:
        return False
    activate_overlay()
    return all(_dist_present(_dist_name(s)) for s in specs)


def _spec_is_safe(spec: str) -> bool:
    """PyPI-by-name only. Re-checked per spec AFTER the allowlist ("belt and
    braces"): a bad row in the allowlist must not reach pip either."""
    s = spec.strip()
    if not s or s.startswith("-"):
        return False
    if any(ch in s for ch in "@;/\\#") or "://" in s:
        return False
    m = _NAME_RE.match(s)
    if not m:
        return False
    rest = s[m.end():]
    if rest and rest[0] not in "[<>=!~":
        return False  # a space, a marker, a hash, a second word
    return bool(_SAFE_SPEC_RE.match(s))


# --- install side ----------------------------------------------------------- #

def _lock_path() -> Optional[Path]:
    """``requirements.lock`` beside the tree (a source/editable install). An
    installed wheel has none — the sdist ships it, the wheel does not."""
    p = Path(__file__).resolve().parent.parent / "requirements.lock"
    return p if p.is_file() else None


def _installed_pins() -> List[str]:
    """The reference scheme, for the no-lock case: pin every installed distribution to
    its current version so a lazy install may ADD packages but never move one."""
    pins: List[str] = []
    for dist in _md.distributions():
        name = (dist.metadata["Name"] or "").strip()
        if name and dist.version:
            pins.append(f"{name}=={dist.version}")
    return sorted(set(pins))


def _constraint_args(specs: Tuple[str, ...]) -> Tuple[List[str], Optional[str]]:
    """``["--constraint", <file>]`` and the temp file to delete afterwards (None
    when the lock itself was used)."""
    lock = _lock_path()
    if lock is not None:
        return ["--constraint", str(lock)], None
    wanted = {_dist_name(s) for s in specs}
    pins = [p for p in _installed_pins() if _dist_name(p) not in wanted]
    fd, tmp = tempfile.mkstemp(prefix="polyrob-lazy-constraints-", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(pins) + "\n")
    return ["--constraint", tmp], tmp


def _installer_env() -> Dict[str, str]:
    """CR-M09: the pip child's env — the host env minus every secret-NAMED var.

    ``tools/code_exec/env_policy.build_child_env`` is the SSOT, but ``core`` may
    not import ``tools``; the name rule is the core-side mirror in
    ``core.app_service.env_scan.SECRET_KEY_RE``. pip keeps its own knobs
    (``PIP_*``, proxies, ``VIRTUAL_ENV``); an ``*_API_KEY`` or the wallet seed
    never reaches a build backend.
    """
    from core.app_service.env_scan import SECRET_KEY_RE
    return {k: v for k, v in os.environ.items() if not SECRET_KEY_RE.search(k)}


def _run_installer(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("env", _installer_env())
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _ask(question: str) -> bool:
    try:
        return input(question).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def ensure(feature: str, *, prompt: bool = True, wait: Optional[float] = None) -> None:
    """Make *feature* importable, installing its extra on first use.

    Order of refusals (each names the remedy and carries ``.kind``): not in the
    allowlist → ``LAZY_DEPS_MODE=off`` → an unsafe spec → (trusted) the route's
    own refusals: a server under custody with no installer unit, a still-dumpable
    custody process, an agent-writable overlay, the installer's named refusal
    (hash mismatch, sdist refused, …), no answer in time → consent declined →
    installed but the distribution is still absent. A feature that is already
    present (base venv or overlay) returns without shelling out.
    """
    specs = LAZY_DEPS.get(feature) if isinstance(feature, str) else None
    if not specs:
        raise FeatureUnavailable(
            f"{feature!r} is not in the allowlist of lazily installable features", "unknown_feature")
    if is_available(feature):
        return
    mode = lazy_mode()
    if mode == "off":
        raise FeatureUnavailable(
            f"{feature} needs {', '.join(_dist_name(s) for s in specs)}, which is not installed. "
            f"Remedy: {remedy(feature)}", "off")
    for spec in specs:
        if not _spec_is_safe(spec):
            raise FeatureUnavailable(
                f"{feature}: unsafe spec {spec!r} refused (PyPI-by-name only)", "unsafe_spec")
    if mode == "legacy":
        _ensure_legacy(feature, specs, prompt=prompt)
        return
    _ensure_trusted(feature, specs, prompt=prompt, wait=wait)


def _custody() -> bool:
    from core.security.host_execution import wallet_custody_enabled
    return wallet_custody_enabled()


def _local_mode() -> bool:
    try:
        from core.config_policy.local_profile import local_mode_enabled
        return local_mode_enabled()
    except Exception:
        return False


def _ensure_trusted(feature: str, specs: Tuple[str, ...], *, prompt: bool,
                    wait: Optional[float]) -> None:
    """066 P1: the hashed closure, into the overlay — by request (server) or in
    process (local)."""
    if spool_provisioned():
        _request_and_wait(feature, SPOOL_WAIT_SEC if wait is None else wait)
    else:
        custody = _custody()
        if custody and not _local_mode():
            raise FeatureUnavailable(
                f"{feature}: this server holds wallet custody and has no trusted installer "
                f"(no {system_root() / 'requests'}); a lazy install is refused here. Remedy: "
                f"provision the root-run installer (the polyrob-deps@ unit), or "
                f"{remedy(feature)}", "no_installer")
        if custody:
            from core.security.process_hardening import custody_reads_closed, hardening_state
            if not custody_reads_closed():
                raise FeatureUnavailable(
                    f"{feature}: lazy install refused while wallet custody is on and this "
                    f"process is still dumpable ({hardening_state().detail}). "
                    f"Remedy: restart the agent, or {remedy(feature)}", "dumpable")
        if prompt and _stdin_is_tty():
            if not _ask(f"{feature} needs {', '.join(specs)} — install the pinned, hashed "
                        f"release files into {local_root()} now? [y/N] "):
                raise FeatureUnavailable(
                    f"{feature}: install declined. Remedy: {remedy(feature)}", "declined")
        # D2: every server and every key holder installs wheels only. Source
        # fallback is reserved for a non-custody local development profile.
        _trusted_install(feature, wheel_only=custody or not _local_mode())
    importlib.invalidate_caches()
    if not is_available(feature):
        raise FeatureUnavailable(
            f"{feature}: the install finished but the distribution is still absent. "
            f"Remedy: {remedy(feature)}", "absent")
    logger.info("lazy_deps: %s installed (trusted)", feature)


def _trusted_install(feature: str, *, wheel_only: bool) -> None:
    """The in-process install (local / OSS). Monkeypatched by tests."""
    from core.lazy_installer import InstallRefused, install
    try:
        install(feature, root=local_root(), wheel_only=wheel_only)
    except InstallRefused as exc:
        raise FeatureUnavailable(f"{exc} Remedy: {remedy(feature)}", exc.kind) from exc


def _overlay_writable() -> Optional[Path]:
    """A provisioned overlay the AGENT can write is a broken install: the point is
    that only ``polyrob-deps`` writes it. Returns the offending path."""
    root = system_root()
    digest = _lock_digest()
    for p in ([root / digest] if digest else []) + [root]:
        if p.is_dir() and os.access(p, os.W_OK):
            return p
    return None


def _request_and_wait(feature: str, wait: float) -> None:
    """Write ``requests/<feature>`` (the name, nothing else) and wait for the
    installer unit to finish the overlay, or to record why it refused."""
    from core.lazy_installer import feature_dir, is_complete, read_failure
    writable = _overlay_writable()
    if writable is not None:
        raise FeatureUnavailable(
            f"{feature}: agent write attempt refused — the overlay {writable} is writable by "
            f"this process, so it is not the read-only overlay the installer guarantees. "
            f"Remedy: re-provision the root-run installer (the polyrob-deps@ unit), which "
            f"leaves the overlay read-only to this process", "overlay_writable")
    digest = _lock_digest()
    if not digest:
        raise FeatureUnavailable(
            f"{feature}: no shipped closure (core/lazy_closures); remedy: {remedy(feature)}",
            "closure_missing")
    root = system_root()
    dest = feature_dir(root, digest, feature)
    started = time.time()
    request = root / "requests" / feature
    try:
        fd = os.open(request, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
        os.close(fd)
        logger.info("lazy_deps: requested %s from the installer unit", feature)
    except FileExistsError:
        pass                                  # already requested; wait for the same answer
    except OSError as exc:
        raise FeatureUnavailable(
            f"{feature}: could not write the install request ({exc}). "
            f"Remedy: {remedy(feature)}", "request_failed") from exc
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if is_complete(dest):
            activate_overlay()
            return
        failure = read_failure(root, digest, feature)
        if failure and float(failure.get("at", 0)) >= started - 1:
            raise FeatureUnavailable(
                f"{feature}: the installer refused ({failure.get('kind')}): "
                f"{str(failure.get('reason', ''))[:400]} Remedy: {remedy(feature)}",
                str(failure.get("kind") or "refused"))
        if time.monotonic() >= deadline:
            raise FeatureUnavailable(
                f"{feature}: the installer unit did not finish within {wait:.0f}s "
                f"(is polyrob-deps.path enabled? `systemctl status polyrob-deps@spool`). "
                f"Remedy: {remedy(feature)}", "timeout")
        time.sleep(_POLL_SEC)


def _ensure_legacy(feature: str, specs: Tuple[str, ...], *, prompt: bool) -> None:
    """Install the release's hashed wheels into this venv, outside custody only."""
    # CR-M09: pip runs sdist build backends as HOST code. Under wallet custody
    # that is the same trust-domain breach git/LSP refuse, so refuse it here too.
    from core.security.host_execution import host_execution_refusal
    refusal = host_execution_refusal()
    if refusal:
        raise FeatureUnavailable(
            f"{feature}: lazy install {refusal}. Remedy: {remedy(feature)}", "custody")
    from core.lazy_closures import read_closure
    try:
        closure = read_closure(feature)
        if closure is None or not closure.pins:
            raise ValueError("missing or empty shipped dependency closure")
    except (OSError, ValueError) as exc:
        raise FeatureUnavailable(
            f"{feature}: no valid release closure. Remedy: {remedy(feature)}",
            "closure_missing") from exc
    if prompt and _stdin_is_tty():
        if not _ask(f"{feature} needs {', '.join(specs)} — install into this venv now? [y/N] "):
            raise FeatureUnavailable(f"{feature}: install declined. Remedy: {remedy(feature)}", "declined")
    # ⚠️ sys.executable -m pip, never a bare `pip`: a systemd unit's PATH is minimal
    # and a bare name resolves to whatever pip is first on it — possibly none, or
    # another interpreter's. Venv-only by construction (the running interpreter).
    constraint, tmp = _constraint_args(specs)
    logger.info("lazy_deps: installing %s (%s)", feature, " ".join(specs))
    try:
        with tempfile.TemporaryDirectory(prefix="polyrob-lazy-release-") as directory:
            requirements = Path(directory) / "requirements.txt"
            requirements.write_text(closure.text, encoding="utf-8")
            cmd = [sys.executable, "-I", "-m", "pip", "install", "--disable-pip-version-check",
                   "--no-input", "--require-hashes", "--no-deps", "--only-binary=:all:",
                   *constraint, "-r", str(requirements)]
            proc = _run_installer(cmd)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
        raise FeatureUnavailable(
            f"{feature}: pip exited {proc.returncode}. Remedy: {remedy(feature)}\n{tail}", "pip_failed")
    importlib.invalidate_caches()
    if not is_available(feature):
        raise FeatureUnavailable(
            f"{feature}: pip reported success but the distribution is still absent. "
            f"Remedy: {remedy(feature)}", "absent")
    logger.info("lazy_deps: %s installed", feature)


def ensure_provider(feature: str) -> None:
    """The thin wrapper an import branch calls before its ``import``: a no-op when
    the SDK is present; otherwise install-or-refuse via :func:`ensure` with no
    TTY prompt (a provider is chosen by config, which is the consent)."""
    if is_available(feature):
        return
    ensure(feature, prompt=False)
