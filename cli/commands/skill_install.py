"""polyrob skill install/approve — safe local skill install (P2, Task 19).

The install pipeline foundation reused by Tasks 20 (git) / 21 (url) / 23 (guard).
DATA-SAFETY critical: it must never lose resource files and must never let
injected content auto-activate.

Pipeline (``install_local``):
  1. resolve src, read/parse SKILL.md, ``validate_consumed`` (lenient loadability
     — reject on an error-level issue).
  2. Reconciliation 1 — strict promotable-id gate: the id must also pass the
     writer's ``validate_skill_id`` (``^[a-z][a-z0-9-]*$``, ≤50), so an install
     can never succeed only to fail at approve. Lenient-named skills are pointed
     at the ``~/.agents/skills`` auto-discovery path instead.
  3. reject symlinks in the source folder (a local folder is UNAUDITED, unlike a
     tree-audited git install).
  4. ``_scan_folder`` — ``is_suspicious`` on SKILL.md + every text resource,
     fail-CLOSED (raise on a hit OR a scanner error).
  5. copy the WHOLE folder (SKILL.md + resources) into ``user_<uid>/.pending/<name>``,
     plus a ``.install-meta.json`` recording the TRUE origin (``source``/
     ``resolved_sha``) so a later bare ``skill approve <name>`` — which only
     knows the skill name, not how it was quarantined — can still audit the
     real source (Task 24).
  6. auto-approve ONLY when ``trust == "local" and source == "local"``; a remote
     (git/url) source is NEVER auto-approved even with ``--trust local``.

Approve (``_approve``): read back ``.install-meta.json`` (Task 24, falls back to
the passed ``source``/``resolved_sha`` if absent) → ``promote_pending_skill``
(promotes+registers+re-scans SKILL.md) → Reconciliation 2 (port the remaining
resource files, excluding the install-meta file, into the ACTIVE skill dir —
``promote_pending_skill`` only moves SKILL.md, so resources would be lost) →
remove the emptied ``.pending/<name>`` → best-effort install audit
(``record_install``, source/sha/approver/ts — never blocks activation).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

_INSTALL_META_NAME = ".install-meta.json"


class InstallError(click.ClickException):
    pass


@dataclass
class InstallResult:
    name: str
    staged_path: Path
    approved: bool
    source: str
    resolved_sha: Optional[str] = None


_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".toml", ".cfg", ""}


def _reject_unsafe_source(folder: Path) -> None:
    """A locally-supplied folder is UNAUDITED. Refuse any symlink (which
    ``copytree`` would dereference, pulling content from OUTSIDE the folder) and
    any entry whose realpath escapes ``folder`` — before we stage anything."""
    root = os.path.realpath(folder)
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            raise InstallError(f"symlink not allowed in skill folder: {p.name}")
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            raise InstallError(f"path escapes skill folder: {p.name}")


def _scan_folder(folder: Path) -> None:
    """Threat-scan SKILL.md + every text resource. Fail-CLOSED: raise on a hit
    OR a scanner error (a write must not slip past a crashing guard). A
    text-suffixed file with invalid UTF-8 bytes is NOT skipped — it is decoded
    with ``errors="replace"`` and scanned anyway, since a bad-UTF-8 ``.md``/
    ``.txt``/etc. still passes the suffix gate and can carry an ASCII-safe
    injection payload alongside the invalid bytes. Only a genuine read failure
    (unreadable/permission) is fail-closed via ``InstallError``."""
    from modules.memory.task.threat_scan import is_suspicious

    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise InstallError(f"cannot read {p.name} for threat scan: {e}")
        text = raw.decode("utf-8", errors="replace")
        try:
            flagged = is_suspicious(text)
        except Exception as e:  # scanner failure => fail-CLOSED
            raise InstallError(f"threat scan failed on {p.name}: {e}")
        if flagged:
            raise InstallError(f"threat scan flagged {p.relative_to(folder)} — install refused")


def _skill_manager():
    from agents.task.agent.skill_manager import get_skill_manager

    return get_skill_manager()


def _require_local_operator() -> None:
    """Skill install is owner/CLI-only. There is no REST install endpoint, and
    this is the single seam (``install_local``) all three install routes
    (``install_local``/``install_git``/``install_url``) funnel through, so one
    guard here covers all of them. Gated on ``local_mode_enabled()`` accessed
    as a module attribute (``constants.local_mode_enabled()``, not a bare
    imported name) so tests can monkeypatch it."""
    from agents.task import constants

    if not constants.local_mode_enabled():
        raise InstallError("skill install is owner/CLI-only; refused on a multi-tenant server")


def install_local(src: Path, *, user_id: str, trust: str = "prompt", source: str = "local",
                  resolved_sha: Optional[str] = None) -> InstallResult:
    _require_local_operator()
    from agents.task.agent.skill_frontmatter import parse_frontmatter
    from agents.task.agent.skill_validation import validate_consumed

    src = Path(src).resolve()
    md = src / "SKILL.md"
    if not md.is_file():
        raise InstallError(f"{src}: no SKILL.md")
    meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
    name = str(meta.get("name") or src.name)

    # Lenient loadability gate (reject only on an error-level issue).
    issues = validate_consumed(meta, src.name)
    errs = [i for i in issues if i.level == "error"]
    if errs:
        raise InstallError(f"{name}: not loadable ({', '.join(i.code for i in errs)})")

    # Reconciliation 1: the id must ALSO be promotable (strict writer regex), so
    # install never succeeds only to fail at approve. Point lenient ids at the
    # ~/.agents/skills auto-discovery path (P1) instead.
    mgr = _skill_manager()
    id_ok, _id_errs = mgr.validate_skill_id(name)
    if not id_ok:
        raise InstallError(
            f"cannot install {name!r}: name must match ^[a-z][a-z0-9-]*$ and be "
            f"<=50 chars to register. Lenient-named skills can still be used by "
            f"placing them in ~/.agents/skills/ (auto-discovered)."
        )

    # A local folder is unaudited — reject symlink/escape before staging.
    _reject_unsafe_source(src)
    # SKILL.md + all text resources, fail-closed.
    _scan_folder(src)

    pending = mgr._user_root(user_id) / ".pending" / name
    if pending.exists():
        shutil.rmtree(pending)
    pending.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, pending)  # whole folder incl. resources
    # Record the TRUE origin alongside the staged skill so a later `skill approve`
    # (Task 24) — which only knows the skill NAME, not how it got quarantined —
    # can audit the real source/sha instead of a hardcoded "local" guess.
    (pending / _INSTALL_META_NAME).write_text(
        json.dumps({"source": source, "resolved_sha": resolved_sha}), encoding="utf-8"
    )

    res = InstallResult(name=name, staged_path=pending, approved=False,
                        source=source, resolved_sha=resolved_sha)
    if trust == "local" and source == "local":  # NEVER auto-approve remote (git/url)
        _approve(name, user_id=user_id, source=source, resolved_sha=resolved_sha)
        res.approved = True
    return res


_MAX_CLONE_BYTES = 25 * 1024 * 1024
_MAX_CLONE_FILES = 5000


def _resolve_git_spec(spec: str):
    """Return ``(clone_url, subdir, ref)``.

    A full URL (``http(s)://``, ``git@host:...``, ``ssh://``, ``file://``) is
    used AS-IS and is NEVER partitioned on ``@`` — an SSH shorthand like
    ``git@github.com:owner/repo.git`` legitimately contains ``@`` as part of the
    URL syntax, not a ref separator. Only a trailing ``/subdir`` after the
    ``.git`` path is split off.

    The bare shorthand ``owner/repo[/subdir][@ref]`` (resolved against GitHub
    over https) DOES support a trailing ``@ref`` — but it is split off the WHOLE
    spec string BEFORE splitting owner/repo/subdir, so
    ``anthropics/skills/pdf@v1.2`` resolves to subdir ``pdf`` / ref ``v1.2``
    rather than leaking ``.git``/ref fragments into the repo path.
    """
    if spec.startswith(("http://", "https://", "git@", "file://", "ssh://")):
        m = re.match(r"^(.*?\.git)(?:/(.+))?$", spec)
        if m:
            return m.group(1), (m.group(2) or ""), None
        # No literal ".git" in the spec (e.g. a bare-repo dir passed as a plain
        # file:// path) — the whole spec is the clone url, no subdir split.
        return spec, "", None
    shorthand, _, ref = spec.partition("@")
    ref = ref or None
    parts = shorthand.split("/")
    if len(parts) < 2:
        raise InstallError(f"unrecognized skill spec: {spec!r}")
    url = f"https://github.com/{parts[0]}/{parts[1]}.git"
    return url, "/".join(parts[2:]), ref


def _reject_symlink_blobs(root: Path, env: Optional[dict] = None) -> None:
    """Reject a symlink at the GIT OBJECT level, not just the filesystem level.

    ``core.symlinks=false`` (set on every clone in ``install_git``) already
    neutralizes a symlink attack by checking a symlink blob (git mode 120000)
    out as an inert plain-text file containing the link target — so
    ``Path.is_symlink()`` on the checked-out tree is NOT a reliable detector
    (it will be False even though the repo committed a real symlink). Ask git
    directly via ``ls-tree`` instead, which reports the object mode regardless
    of how core.symlinks materialized it on disk.

    This is the ONLY effective symlink detector in that scenario, so a failed
    or crashing ``ls-tree`` call must FAIL-CLOSED (refuse the install) rather
    than silently no-op — a best-effort "return on error" here would let a
    symlink-bearing clone slip through disguised as an audit failure."""
    if not (root / ".git").exists():
        return  # not a git worktree (e.g. exercised directly in a unit test)
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--full-tree", "HEAD"],
                              env=env, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise InstallError(f"cannot audit clone for symlinks (git ls-tree failed) — refusing: {e}")
    if proc.returncode != 0:
        raise InstallError(
            "cannot audit clone for symlinks (git ls-tree failed) — refusing: "
            f"{proc.stderr.strip() or proc.returncode}"
        )
    for line in proc.stdout.splitlines():
        mode = line.split(None, 1)[0] if line else ""
        if mode == "120000":
            path = line.split("\t", 1)[-1]
            raise InstallError(f"symlink refused in git tree: {path}")


def _audit_tree(root: Path, env: Optional[dict] = None) -> None:
    """Additional, EARLIER defense over the raw git clone (before any subdir
    selection or handoff to ``install_local``): reject any symlink (both at the
    git-object level via ``_reject_symlink_blobs`` AND the filesystem level, in
    case symlinks were ever materialized), reject any path whose realpath
    escapes the clone root (traversal), and enforce a byte cap AND a
    file-count cap so a malicious/oversized repo cannot be staged."""
    root = root.resolve()
    _reject_symlink_blobs(root, env=env)
    total = files = 0
    for p in root.rglob("*"):
        if p.is_symlink():
            raise InstallError(f"symlink refused in git clone: {p.relative_to(root)}")
        try:
            rp = p.resolve()
            rp.relative_to(root)
        except (ValueError, OSError):
            raise InstallError(f"path escapes clone root: {p}")
        if p.is_file():
            files += 1
            total += p.stat().st_size
            if files > _MAX_CLONE_FILES or total > _MAX_CLONE_BYTES:
                raise InstallError("clone exceeds size/file caps — refused")


def install_git(spec: str, *, user_id: str, ref: Optional[str] = None,
                trust: str = "prompt") -> InstallResult:
    """Resolve a git URL or ``owner/repo[/subdir]`` shorthand, clone it into a
    sandboxed temp dir (no credential helper, no hooks, no interactive prompt,
    shallow + single-branch, wall-clock timeout), audit the raw tree BEFORE
    selecting the subdir, record the resolved commit SHA, and hand the skill
    folder to ``install_local`` with ``source=f"git:{spec}"`` — which guarantees
    quarantine (``install_local``'s auto-approve gate only fires for
    ``source == "local"``, so a git install can never auto-approve even with
    ``trust="local"``)."""
    url, subdir, spec_ref = _resolve_git_spec(spec)
    if ref is None:
        ref = spec_ref
    if subdir and Path(subdir).is_absolute():
        raise InstallError("invalid subdir")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_GLOBAL="/dev/null")
    with tempfile.TemporaryDirectory(prefix="polyrob-skill-") as tmp:
        clone = Path(tmp) / "clone"
        cmd = ["git", "-c", "core.symlinks=false", "-c", "core.hooksPath=", "clone",
               "--depth", "1", "--single-branch", "--no-recurse-submodules"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [url, str(clone)]
        try:
            subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise InstallError(f"git clone failed: {e}")

        sha_proc = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                                  env=env, capture_output=True, text=True, timeout=30)
        if sha_proc.returncode != 0:
            raise InstallError(f"git rev-parse failed: {sha_proc.stderr.strip() or sha_proc.returncode}")
        sha = sha_proc.stdout.strip()

        _audit_tree(clone, env=env)  # symlink/traversal/cap audit — BEFORE subdir selection

        clone_real = clone.resolve()
        skill_dir = (clone / subdir) if subdir else clone
        skill_dir = skill_dir.resolve()
        if skill_dir != clone_real and clone_real not in skill_dir.parents:
            raise InstallError("subdir escapes clone root")

        return install_local(skill_dir, user_id=user_id, trust=trust,
                             source=f"git:{spec}", resolved_sha=sha)  # remote => never auto-approve


_MAX_URL_BYTES = 512 * 1024


def _fetch_text(url: str, *, max_bytes: int = _MAX_URL_BYTES, timeout: int = 30) -> str:
    """Fetch a single SKILL.md over HTTP(S) with a size cap, a wall-clock
    timeout, and a content-type gate (text/markdown/octet-stream/empty only —
    reject anything else, e.g. an HTML error page masquerading as a 200).

    Module-level so tests can monkeypatch it (``skill_install._fetch_text``)
    without any network access; ``install_url`` MUST call it as a bare
    module-level function (not inlined) to keep that seam monkeypatchable."""
    import urllib.parse
    import urllib.request

    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise InstallError(f"unsupported URL scheme: {scheme!r} (only http/https)")

    req = urllib.request.Request(url, headers={"User-Agent": "polyrob-skill-install"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec - operator-initiated
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and not any(t in ctype for t in ("text/", "markdown", "application/octet-stream")):
            raise InstallError(f"refusing non-text content-type: {ctype!r}")
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise InstallError(f"SKILL.md exceeds {max_bytes} bytes")
    return data.decode("utf-8", errors="replace")


def install_url(url: str, *, user_id: str, trust: str = "prompt") -> InstallResult:
    """Fetch a single ``SKILL.md`` from ``url``, stage it into a temp folder
    named by its frontmatter ``name`` (fallback ``downloaded-skill``), and
    hand it to ``install_local`` with ``source=f"url:{url}"`` — which
    guarantees quarantine (``install_local``'s auto-approve gate only fires
    for ``source == "local"``, so a URL install can never auto-approve even
    with ``trust="local"``)."""
    from agents.task.agent.skill_frontmatter import parse_frontmatter

    text = _fetch_text(url)
    meta, _ = parse_frontmatter(text)
    name = str(meta.get("name") or "downloaded-skill")
    # The frontmatter `name` comes from UNTRUSTED fetched content and is about to be
    # joined into a filesystem path — reject anything that could escape the temp
    # staging dir (traversal, absolute path, path separators) BEFORE any write.
    if not name or "/" in name or "\\" in name or ".." in name or Path(name).is_absolute():
        raise InstallError(f"refusing skill name from URL: {name!r}")
    with tempfile.TemporaryDirectory(prefix="polyrob-url-") as tmp:
        d = Path(tmp) / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        return install_local(d, user_id=user_id, trust=trust, source=f"url:{url}")  # never auto-approve


def _looks_like_skill_md_url(spec: str) -> bool:
    """A direct fetchable ``SKILL.md`` URL: http(s) AND the path (query/fragment
    stripped) ends with ``SKILL.md`` — distinct from a git/GitHub URL, which
    routes through ``install_git`` instead."""
    if not spec.startswith(("http://", "https://")):
        return False
    path = spec.split("?", 1)[0].split("#", 1)[0]
    return path.rstrip("/").endswith("SKILL.md")


# ``owner/repo[/subdir]``, optionally with a trailing ``@ref`` (the same
# ``@ref`` shorthand ``_resolve_git_spec`` understands) — deliberately strict
# (no leading ``.``/``/``, no whitespace) so a mistyped LOCAL path never looks
# like a plausible GitHub shorthand.
_GIT_SHORTHAND_RE = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._/-]+)?(?:@[A-Za-z0-9._/-]+)?$"
)


def _looks_like_git_spec(spec: str) -> bool:
    """Only route to ``install_git`` for an EXPLICIT git/URL scheme or a strict
    ``owner/repo[/subdir][@ref]`` shorthand. A spec that merely *fails* to be a
    local dir or a SKILL.md URL is otherwise NOT assumed to be a git spec —
    that used to send a fat-fingered local path (e.g. ``./nope/typo``) into a
    slow, confusing ~120s network clone against a bogus GitHub URL. See Task 22
    review finding 2."""
    if spec.startswith(("http://", "https://", "git@", "ssh://", "file://")):
        return True
    if spec.startswith((".", "/")):
        return False
    return bool(_GIT_SHORTHAND_RE.match(spec))


def dispatch_install(spec: str, *, user_id: str, trust: str = "prompt",
                     ref: Optional[str] = None) -> InstallResult:
    """Route a single install *spec* string to the right pipeline by shape
    (single entry point for both ``polyrob skill install`` and the REPL's
    ``/skills install``):

      * an existing local directory                     -> ``install_local``
      * a direct http(s) URL ending in ``SKILL.md``      -> ``install_url``
      * a git/URL scheme or ``owner/repo[/subdir][@ref]``
        shorthand                                        -> ``install_git``
      * anything else                                    -> ``InstallError``
        (a fast, clear error instead of a slow, confusing network clone
        attempt against a spec that was never a git reference to begin with)

    ``ref`` is only meaningful for the git path (ignored otherwise).
    """
    if Path(spec).is_dir():
        return install_local(Path(spec), user_id=user_id, trust=trust)
    if _looks_like_skill_md_url(spec):
        return install_url(spec, user_id=user_id, trust=trust)
    if _looks_like_git_spec(spec):
        return install_git(spec, user_id=user_id, ref=ref, trust=trust)
    raise InstallError(
        f"unrecognized skill spec: {spec!r} — expected a local directory, a "
        f"SKILL.md URL, or an owner/repo[/subdir] / git URL"
    )


def _port_resources(pending_dir: Path, active_dir: Path) -> None:
    """Copy the staged resource files/subdirs (everything except the already-promoted
    SKILL.md) from ``.pending/<name>`` into the ACTIVE skill dir. Realpath-confined:
    a copied file may never land outside ``active_dir``."""
    active_dir.mkdir(parents=True, exist_ok=True)
    active_real = os.path.realpath(active_dir)
    for item in sorted(pending_dir.iterdir()):
        if item.name == "SKILL.md":
            continue  # already promoted+registered by promote_pending_skill
        if item.name == _INSTALL_META_NAME:
            continue  # install-provenance metadata, not skill content
        dest = active_dir / item.name
        dest_real = os.path.realpath(dest)
        if dest_real != active_real and not dest_real.startswith(active_real + os.sep):
            continue  # confinement guard — never write outside the active dir
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)


def _read_install_meta(pending_dir: Path, *, source: str,
                       resolved_sha: Optional[str]) -> tuple:
    """Recover the TRUE install origin recorded at quarantine time (Task 24).

    ``skill approve`` (the CLI command) only knows the skill *name* — it calls
    ``_approve(name, source="local")`` unconditionally, because at approve time
    it has no idea whether the quarantined skill actually came from a git repo
    or a URL. ``install_local`` stamps ``.install-meta.json`` into the pending
    dir with the real source/sha at staging time; read it back here so a git/url
    install that was later approved via the bare CLI command is still audited
    under its real origin instead of being misattributed as "local". Falls back
    to the passed-in ``source``/``resolved_sha`` when the file is absent
    (e.g. a pending dir staged before this metadata existed) or unreadable."""
    meta_path = pending_dir / _INSTALL_META_NAME
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("source", source), meta.get("resolved_sha", resolved_sha)
        except Exception:
            pass
    return source, resolved_sha


def _approve(name: str, *, user_id: str, source: str = "local",
             resolved_sha: Optional[str] = None) -> None:
    """Promote ``user_<uid>/.pending/<name>`` to active via the writer pipeline,
    port its resource files (Reconciliation 2 — promote only moves SKILL.md), clean
    up the staging dir, and record the install audit row (Task 24)."""
    _require_local_operator()
    mgr = _skill_manager()
    pending_dir = mgr._user_root(user_id) / ".pending" / name
    active_dir = mgr._user_root(user_id) / name

    # Recover the true origin BEFORE promote_pending_skill/_port_resources touch
    # (and eventually delete) the pending dir.
    audit_source, audit_sha = _read_install_meta(
        pending_dir, source=source, resolved_sha=resolved_sha)

    res = mgr.promote_pending_skill(name, user_id=user_id)  # promotes+registers+re-scans SKILL.md
    if not getattr(res, "ok", False):
        errs = ", ".join(getattr(res, "errors", []) or []) or "unknown error"
        raise InstallError(f"approve failed for {name!r}: {errs}")

    # Reconciliation 2: promote_pending_skill only moves SKILL.md — port the rest
    # (references/assets) so approved skills keep their resources, then drop staging.
    if pending_dir.exists():
        _port_resources(pending_dir, active_dir)
        shutil.rmtree(pending_dir, ignore_errors=True)

    try:  # audit is best-effort — must never block activation.
        from modules.skills.skill_usage import get_skill_usage_store

        get_skill_usage_store().record_install(
            name, user_id=user_id, source=audit_source, resolved_sha=audit_sha,
            approver=user_id)
    except Exception:
        pass


def list_all_skills(user_id: str) -> List[Dict[str, Any]]:
    """Enumerate every skill visible to *user_id*, across scope and state.

    Scopes: ``builtin`` (protected ids shipped with polyrob), ``user`` (this
    tenant's own skills under ``user_<uid>/``), ``external:<scope>``
    (agentskills.io-style auto-discovery, e.g. ``~/.agents/skills``). States:
    ``active`` / ``pending`` (``.pending/``, staged by ``install`` awaiting
    ``approve``) / ``archived`` (``.archived/``, retired by ``remove``).

    ``source``/``sha`` are attached best-effort from
    ``skill_usage.get_provenance`` when available; that store only has
    provenance (author) rows, not the install-audit trail (``record_install``/
    ``list_installs`` — see ``skill_usage.py``), so this degrades gracefully
    when neither is populated rather than needing an update.
    """
    mgr = _skill_manager()
    rows: List[Dict[str, Any]] = []
    seen_ids = set()

    try:
        from agents.task.agent import skill_store

        for sid in sorted(skill_store.builtin_skill_ids()):
            rows.append({"id": sid, "scope": "builtin", "status": "active"})
            seen_ids.add(sid)
    except Exception:
        pass

    user_root = mgr._user_root(user_id)
    archived_entries: List[Path] = []
    if user_root.is_dir():
        for d in sorted(user_root.iterdir()):
            if not d.is_dir():
                continue
            if d.name == ".pending":
                for pd in sorted(d.iterdir()):
                    if pd.is_dir() and (pd / "SKILL.md").is_file():
                        rows.append({"id": pd.name, "scope": "user", "status": "pending"})
                continue
            if d.name == ".archived":
                # Deferred to AFTER the full active-dir scan below (not emitted
                # inline here) — ``.archived`` sorts before a normal skill-name
                # dir alphabetically, so checking ``seen_ids`` at this point in
                # the loop would run before the matching ``active`` id (if any)
                # has been recorded, defeating the dedupe.
                archived_entries = sorted(d.iterdir())
                continue
            if (d / "SKILL.md").is_file():
                rows.append({"id": d.name, "scope": "user", "status": "active"})
                seen_ids.add(d.name)

        # A remove-then-reinstall of the same id leaves BOTH an active dir and
        # a stale ``.archived/<id>`` copy — only surface the archived row when
        # that id isn't otherwise active/present (Task 22 review finding 3).
        for ad in archived_entries:
            if ad.is_dir() and ad.name not in seen_ids:
                rows.append({"id": ad.name, "scope": "user", "status": "archived"})
                seen_ids.add(ad.name)

    try:
        for ext_id, ds in mgr._load_external_skills().items():
            if ext_id in seen_ids:
                continue
            rows.append({"id": ext_id, "scope": f"external:{getattr(ds, 'scope', '?')}", "status": "active"})
            seen_ids.add(ext_id)
    except Exception:
        pass

    try:
        from modules.skills.skill_usage import get_skill_usage_store

        store = get_skill_usage_store()
        for row in rows:
            try:
                prov = store.get_provenance(row["id"], user_id)
            except Exception:
                prov = None
            if prov:
                if prov.get("source"):
                    row["source"] = prov["source"]
                if prov.get("resolved_sha"):
                    row["sha"] = prov["resolved_sha"]
    except Exception:
        pass

    return rows


def _traversal_safe(skill_id: str) -> bool:
    """The SAME traversal guard ``SkillManager.resolve_skill_dir`` applies
    before it joins ``skill_id`` under a tenant directory — deliberately
    looser than ``validate_skill_id`` (must still accept legit lenient
    external ids like ``3d-modeling``), just enough to refuse anything that
    could escape the ``.pending``/``.archived`` directory join below (Task 22
    review finding 1: an unvalidated ``skill_id`` there let one tenant read a
    SIBLING tenant's quarantined/archived skill via ``../../user_<other>/...``)."""
    return bool(
        skill_id
        and "/" not in skill_id
        and "\\" not in skill_id
        and ".." not in skill_id
        and not Path(skill_id).is_absolute()
    )


def get_skill_info(skill_id: str, user_id: str) -> Dict[str, Any]:
    """Frontmatter + provenance + usage metrics for *skill_id*, wherever it
    lives (active via ``resolve_skill_dir``, quarantined under ``.pending/``,
    or retired under ``.archived/``). Raises ``InstallError`` if not found in
    any of those — including when ``skill_id`` fails the traversal guard (an
    unsafe id is treated as not-found, never used to construct a path).
    Provenance/usage lookups are fail-open (absent store/rows degrade to
    omitted fields, never raise)."""
    from agents.task.agent.skill_frontmatter import parse_frontmatter

    mgr = _skill_manager()
    skill_dir = mgr.resolve_skill_dir(skill_id, user_id=user_id)
    status = "active"
    if skill_dir is None and _traversal_safe(skill_id):
        pending = mgr._user_root(user_id) / ".pending" / skill_id
        if (pending / "SKILL.md").is_file():
            skill_dir, status = pending, "pending"
    if skill_dir is None and _traversal_safe(skill_id):
        archived = mgr._user_root(user_id) / ".archived" / skill_id
        if (archived / "SKILL.md").is_file():
            skill_dir, status = archived, "archived"
    if skill_dir is None:
        raise InstallError(f"skill {skill_id!r} not found (checked active/pending/archived)")

    md = skill_dir / "SKILL.md"
    meta: Dict[str, Any] = {}
    if md.is_file():
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))

    info: Dict[str, Any] = {
        "id": skill_id,
        "status": status,
        "path": str(skill_dir),
        "name": meta.get("name", skill_id),
        "description": meta.get("description", ""),
        "license": meta.get("license", ""),
    }
    try:
        from modules.skills.skill_usage import get_skill_usage_store

        store = get_skill_usage_store()
        prov = store.get_provenance(skill_id, user_id)
        usage = store.get_usage(skill_id, user_id)
        if prov:
            info["created_by"] = prov.get("created_by")
            info["created_at"] = prov.get("created_at")
        info["load_count"] = usage.get("load_count", 0)
        info["last_used_at"] = usage.get("last_used_at")
    except Exception:
        pass
    return info


def remove_skill(skill_id: str, user_id: str) -> bool:
    """Archive (never hard-delete) a user skill. Thin wrapper over
    ``SkillWriterMixin.delete_skill`` — a builtin/external/unknown id (or one
    a background/non-user author isn't allowed to touch) returns ``False``."""
    return _skill_manager().delete_skill(skill_id, user_id=user_id)


# --- CLI group -------------------------------------------------------------

def _default_user() -> str:
    """The local owner id, mirroring `polyrob owner invite`'s resolution."""
    try:
        from core.instance import resolve_owner_principal

        return resolve_owner_principal() or "local"
    except Exception:
        return "local"


@click.group("skill")
def skill():
    """Install and approve agent skills (single-skill install pipeline)."""
    pass


@skill.command("install")
@click.argument("spec")
@click.option("--ref", default=None,
              help="Git ref (branch/tag/commit) — only used when SPEC resolves to a git install.")
@click.option("--trust", type=click.Choice(["local", "prompt"]), default="prompt",
              help="'local' auto-approves a local folder; 'prompt' quarantines for `skill approve`. "
                   "A git/url install is NEVER auto-approved, even with --trust local.")
@click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
def skill_install(spec: str, ref: Optional[str], trust: str, user_id: Optional[str]):
    """Install a skill from a local folder, a git repo, or a direct SKILL.md URL.

    SPEC is dispatched by shape: an existing local directory installs locally;
    an http(s) URL ending in SKILL.md fetches that single file; anything else
    (a git URL or an `owner/repo[/subdir][@ref]` GitHub shorthand) clones via git.
    """
    uid = user_id or _default_user()
    res = dispatch_install(spec, user_id=uid, trust=trust, ref=ref)
    if res.approved:
        click.echo(click.style("[polyrob] ", fg="green")
                   + f"installed + approved skill {res.name!r} (active).")
    else:
        click.echo(click.style("[polyrob] ", fg="yellow")
                   + f"installed skill {res.name!r} to quarantine — run "
                     f"`polyrob skill approve {res.name} --user {uid}` to activate.")
        click.echo(f"  staged: {res.staged_path}")


@skill.command("approve")
@click.argument("name")
@click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
def skill_approve(name: str, user_id: Optional[str]):
    """Approve (activate) a quarantined skill previously installed with --trust prompt."""
    uid = user_id or _default_user()
    _approve(name, user_id=uid, source="local")
    click.echo(click.style("[polyrob] ", fg="green") + f"approved skill {name!r} (active).")


@skill.command("list")
@click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
def skill_list_cmd(user_id: Optional[str]):
    """List every skill visible to this user: builtin/user/external scope, active/pending/archived state."""
    uid = user_id or _default_user()
    rows = list_all_skills(uid)
    if not rows:
        click.echo("No skills found.")
        return
    for row in rows:
        extra = ""
        if row.get("source"):
            extra += f" source={row['source']}"
        if row.get("sha"):
            extra += f" sha={row['sha'][:8]}"
        click.echo(f"{row['id']:<28} {row['scope']:<16} {row['status']:<10}{extra}")


@skill.command("info")
@click.argument("skill_id")
@click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
def skill_info_cmd(skill_id: str, user_id: Optional[str]):
    """Show frontmatter + provenance/usage metrics for a single skill."""
    uid = user_id or _default_user()
    info = get_skill_info(skill_id, uid)
    click.echo(f"id: {info['id']}")
    click.echo(f"status: {info['status']}")
    click.echo(f"path: {info['path']}")
    click.echo(f"name: {info['name']}")
    click.echo(f"description: {info['description']}")
    if info.get("license"):
        click.echo(f"license: {info['license']}")
    if "created_by" in info:
        click.echo(f"created_by: {info['created_by']}")
        click.echo(f"created_at: {info['created_at']}")
    if "load_count" in info:
        click.echo(f"load_count: {info['load_count']}")
        click.echo(f"last_used_at: {info['last_used_at']}")


@skill.command("remove")
@click.argument("skill_id")
@click.option("--user", "user_id", default=None, help="Tenant user_id (default: local owner id).")
def skill_remove_cmd(skill_id: str, user_id: Optional[str]):
    """Archive (never hard-delete) a user skill."""
    uid = user_id or _default_user()
    ok = remove_skill(skill_id, uid)
    if not ok:
        raise InstallError(
            f"could not remove skill {skill_id!r} (not found, builtin/external, or not permitted)"
        )
    click.echo(click.style("[polyrob] ", fg="green")
               + f"removed skill {skill_id!r} (archived under .archived/ — recoverable).")
