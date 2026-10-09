"""``polyrob pack`` — installed packs (067 P2).

``list`` / ``info`` / ``doctor`` read what the loader found; ``enable`` /
``disable`` edit ``POLYROB_PACKS`` / ``POLYROB_PACKS_DISABLED`` through the one
config write path and apply in a new process, subject to env precedence (a
running process keeps its pack state); ``remove`` prints the pip command.

067 P7: ``search`` reads the curated index (``core.packs.index``, local, no
network). ``install <id>`` resolves ONLY index ids: a first-party pack ships
inside polyrob (067, one install), so its install is its SDK extra (trusted lazy
install when one ships, else the named command); a third-party row installs its
exact pinned, hash-checked wheel. ``install
git+https://…@<40-hex>`` or ``install <path>`` is a THIRD-PARTY pack: its declared
capabilities are shown and must be accepted, it is refused under wallet custody
and on the kill list, and it installs with ``pip --no-deps`` (its dependencies
are printed, never installed silently). ``enable`` refuses a killed pack.

Also here: :class:`PackCommand`, the stand-in the root group returns for a
command a pack declares in its ``pack.toml`` (``cli/polyrob.py``).
"""
import re
import sys
from pathlib import Path

import click

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _loaded_state():
    """Load env + packs exactly as a session would, then return the state module."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.packs import state
    from core.packs.loader import load_packs
    load_packs()
    return state


def _valid_id(pack_id: str) -> str:
    if not _ID_RE.match(pack_id or ""):
        raise click.ClickException(f"{pack_id!r} is not a pack id ({_ID_RE.pattern})")
    return pack_id


@click.group("pack")
def pack():
    """Installed packs: list, inspect, enable, disable, install."""


@pack.command("list")
def pack_list():
    """Every installed pack and its state."""
    state = _loaded_state()
    recs = state.records()
    if state.discovery_error():
        click.echo(f"! discovery failed: {state.discovery_error()}")
    if not recs:
        click.echo("no packs installed")
        return
    for rec in recs:
        click.echo(rec.line())


@pack.command("info")
@click.argument("pack_id")
def pack_info(pack_id):
    """What a pack declares (read from its pack.toml) and its state."""
    state = _loaded_state()
    rec = state.record(_valid_id(pack_id))
    if rec is None:
        raise click.ClickException(f"pack {pack_id!r} is not installed")
    click.echo(rec.line())
    click.echo(f"entry point: {rec.entry_point}")
    m = rec.manifest
    if m is None:
        return
    click.echo(f"summary: {m.summary or '-'}")
    click.echo(f"manifest: {m.path}")
    click.echo(f"pack_api: {m.pack_api} · requires_core: {m.requires_core or 'any'} · "
               f"requires_packs: {', '.join(m.requires_packs) or 'none'}")
    click.echo(f"capabilities: {', '.join(sorted(m.capabilities)) or 'none'}")
    for tool in m.tools:
        caps = ", ".join(sorted(tool.row)) or "no special capabilities"
        click.echo(f"tool {tool.id}: {caps}")
        for verb in tool.verbs:
            extra = ", ".join(f"{k}={v}" for k, v in sorted(verb.fields_set().items()))
            click.echo(f"  {verb.name}: {extra or 'inherit'}")
    if m.cli_commands:
        click.echo(f"cli: {', '.join(m.cli_commands)}")


@pack.command("doctor")
def pack_doctor():
    """Refusals and errors, with reasons. Exits 1 when a pack is refused."""
    import os
    state = _loaded_state()
    problems = 0
    if state.discovery_error():
        click.echo(f"! discovery failed: {state.discovery_error()}")
        problems += 1
    installed = {r.id for r in state.records()}
    from core.packs.loader import csv_ids
    for name in csv_ids(os.environ.get("POLYROB_PACKS")):
        if name not in installed:
            click.echo(f"! POLYROB_PACKS names {name!r}, which is not installed")
    for rec in state.records():
        click.echo(rec.line())
        problems += rec.status == state.REFUSED
    if not state.records():
        click.echo("no packs installed")
    if problems:
        sys.exit(1)


def _write_flag(key: str, ids) -> None:
    from core.config_service import set_value
    result = set_value(key, ",".join(sorted(ids)), scope="global")
    if not result.ok:
        raise click.ClickException(result.message)
    for note in result.notes:
        click.echo(note)


@pack.command("enable")
@click.argument("pack_id")
def pack_enable(pack_id):
    """Save enablement for a new process (subject to config precedence)."""
    import os
    state = _loaded_state()
    rec = state.record(_valid_id(pack_id))
    if rec is None:
        raise click.ClickException(f"pack {pack_id!r} is not installed")
    from core.packs.loader import csv_ids, kill_reason
    killed = kill_reason(rec)
    if killed:
        raise click.ClickException(f"pack {pack_id} cannot be enabled: {killed}")
    disabled = set(csv_ids(os.environ.get("POLYROB_PACKS_DISABLED")))
    named = set(csv_ids(os.environ.get("POLYROB_PACKS")))
    if pack_id in disabled:
        _write_flag("POLYROB_PACKS_DISABLED", disabled - {pack_id})
    if named and pack_id not in named:
        _write_flag("POLYROB_PACKS", named | {pack_id})
    elif not named and rec.tier != "first-party":
        # Naming a pack in POLYROB_PACKS turns the default ("every first-party
        # pack") into an explicit list: keep today's first-party set, add this one.
        first = {r.id for r in state.records() if r.tier == "first-party"}
        _write_flag("POLYROB_PACKS", first | {pack_id})
    click.echo(f"pack {pack_id} enable setting saved — active in a new process unless overridden")


@pack.command("disable")
@click.argument("pack_id")
def pack_disable(pack_id):
    """Save disablement for a new process (subject to config precedence)."""
    import os
    from core.packs.loader import csv_ids
    _valid_id(pack_id)
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    disabled = set(csv_ids(os.environ.get("POLYROB_PACKS_DISABLED")))
    _write_flag("POLYROB_PACKS_DISABLED", disabled | {pack_id})
    click.echo(f"pack {pack_id} disable setting saved — active in a new process unless overridden")


_GIT_RE = re.compile(r"^git\+(https://[^@\s]+)@([0-9a-fA-F]{40})$")


@pack.command("search")
@click.argument("query", required=False, default="")
def pack_search(query):
    """Search the curated pack index (a local file; no network)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.packs import index
    rows = index.search(query)
    if not rows:
        click.echo(f"no pack in the index matches {query!r}" if query else "the pack index is empty")
        return
    for row in rows:
        caps = ", ".join(row.get("capabilities", [])) or "none"
        line = (f"{row['id']} {row['version']} ({row['tier']}) — {row.get('summary') or '-'}"
                f" · capabilities: {caps} · {row['dist']}")
        killed = index.killed(row["id"], row["version"])
        click.echo(line + (f" · ! {killed}" if killed else ""))


@pack.command("install")
@click.argument("source")
@click.option("--accept-capabilities", is_flag=True,
              help="Accept a third-party pack's declared capabilities without a prompt.")
def pack_install(source, accept_capabilities):
    """Install a pack: an index id, git+https://…@<40-hex sha>, or a local path."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    if source.startswith("git+"):
        m = _GIT_RE.match(source)
        if not m:
            raise click.ClickException(
                "a git pack install pins a commit: git+https://<host>/<repo>@<40-hex sha> "
                "(a branch or tag can move)")
        _refuse_under_custody()
        import tempfile
        from cli.commands.skill_install import clone_audited
        with tempfile.TemporaryDirectory(prefix="polyrob-pack-") as tmp:
            clone = Path(tmp) / "clone"
            sha = clone_audited(m.group(1), clone, sha=m.group(2).lower())
            _install_third_party(clone, f"{m.group(1)}@{sha}", accept_capabilities)
        return
    if "/" in source or source.startswith(".") or Path(source).is_dir():
        path = Path(source).expanduser()
        if not path.is_dir():
            raise click.ClickException(f"{source} is not a directory")
        _refuse_under_custody()
        _install_third_party(path.resolve(), str(path.resolve()), accept_capabilities)
        return
    _install_from_index(_valid_id(source), accept_capabilities)


def _refuse_under_custody() -> None:
    import os
    if getattr(os, "geteuid", lambda: -1)() == 0:
        raise click.ClickException(
            "third-party pack installation as root is refused: build backends and "
            "installed Python hooks execute code; use a separate unprivileged instance")
    from core.packs.loader import custody_refusal
    reason = custody_refusal()
    if reason:
        raise click.ClickException(reason)


def _pip(args) -> None:
    """``python -m pip install <args>`` in this environment, scrubbed env."""
    import subprocess
    from core.lazy_installer import child_env
    cmd = [sys.executable, "-I", "-m", "pip", "install", "--disable-pip-version-check", *args]
    proc = subprocess.run(cmd, env=child_env(), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise click.ClickException(f"pip install failed:\n{(proc.stderr or proc.stdout)[-2000:]}")


def _accept(accepted: bool) -> None:
    if accepted:
        return
    if sys.stdin.isatty() and click.confirm("Install this third-party pack with these capabilities?"):
        return
    raise click.ClickException("not installed: re-run with --accept-capabilities to accept "
                               "the declared capabilities")


def _install_from_index(pack_id: str, accepted: bool) -> None:
    from core.packs import index
    row = index.find(pack_id)
    if row is None:
        raise click.ClickException(f"pack {pack_id!r} is not in the pack index "
                                   "(see: polyrob pack search)")
    killed = index.killed(row["id"], row["version"]) or index.killed(row["dist"], row["version"])
    if killed:
        raise click.ClickException(f"pack {pack_id} {row['version']} is refused: {killed}")
    pin = f"{row['dist']}=={row['version']}"
    if row["tier"] == "third-party":
        _refuse_under_custody()
        click.echo(f"third-party pack {pack_id} {row['version']} ({row['dist']})")
        click.echo(f"declared capabilities: {', '.join(row.get('capabilities', [])) or 'none'}")
        _accept(accepted)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as req:
            req.write(f"{pin} --hash=sha256:{row['sha256']}\n")
        try:
            _pip(["--no-deps", "--require-hashes", "-r", req.name])
        finally:
            Path(req.name).unlink(missing_ok=True)
        click.echo(f"pack {pack_id} installed (--no-deps). Name it in POLYROB_PACKS to load it: "
                   f"polyrob pack enable {pack_id}")
        return
    _install_first_party(pack_id)


def _install_first_party(pack_id: str) -> None:
    """067 (one install): a first-party pack ships INSIDE polyrob — there is no
    second distribution to install. What may be missing is its SDK extra: name
    it, or install it through the trusted lazy installer when that extra has a
    hashed lazy closure (e.g. anysite)."""
    state = _loaded_state()
    rec = state.record(pack_id)
    if rec is None or rec.manifest is None:
        raise click.ClickException(
            f"pack {pack_id} ships inside polyrob, but this install does not provide it"
            + (f" ({rec.reason})" if rec is not None and rec.reason else "")
            + "; reinstall polyrob (pip install -e . in a source tree)")
    from core.packs.sdk import needs
    missing = needs(rec.manifest)
    extra = rec.manifest.extra
    if not missing:
        click.echo(f"pack {pack_id} ships inside polyrob and is complete"
                   + (f" (its [{extra}] extra is installed)" if extra else "")
                   + f": {rec.line()}")
        return
    from core.lazy_deps import FeatureUnavailable, ensure, feature_for_extra
    feature = feature_for_extra(extra) if extra else None
    if feature is not None:
        try:
            ensure(feature)
        except FeatureUnavailable as exc:
            raise click.ClickException(str(exc))
        click.echo(f"pack {pack_id}: the [{extra}] extra is installed — active from the next session")
        return
    remedy = missing[0].remedy()
    click.echo(f"pack {pack_id} ships inside polyrob; its SDKs are the [{extra}] extra, "
               f"which is not installed (tools withheld: {', '.join(n.tool for n in missing)}).")
    click.echo(f"Install it: {remedy}")
    click.echo(f"From a polyrob source tree, hash-checked: install.sh --packs {pack_id} "
               f"(or polyrob update --apply).")
    sys.exit(1)


def _find_manifest(root: Path) -> Path:
    """The one ``pack.toml`` of a source tree (in a top-level package directory)."""
    found = sorted(p for p in root.glob("*/pack.toml")) + sorted(root.glob("src/*/pack.toml"))
    if len(found) != 1:
        raise click.ClickException(f"expected exactly one <package>/pack.toml in the source, "
                                   f"found {len(found)}")
    return found[0]


def wheel_refusal(names, package: str, dist: str) -> "str | None":
    """Why a third-party pack wheel's file list must not reach the shared venv.

    The pack installs into the venv every polyrob process (and the signer)
    imports from, so it may add exactly its own package and its metadata: no
    ``.pth`` / ``__editable__`` start-up hook, no ``.data`` scheme (scripts,
    headers, purelib remaps) and no second top-level name that could replace
    polyrob or a dependency (SUP-3)."""
    from packaging.utils import canonicalize_name
    want = canonicalize_name(dist or "")
    for name in names:
        top = name.split("/", 1)[0]
        if top.endswith(".dist-info"):
            if canonicalize_name(top[:-len(".dist-info")].rsplit("-", 1)[0]) != want:
                return f"foreign metadata directory {top}"
            continue
        if top != package:
            return f"file outside the pack's package {package!r}: {name}"
        if name.endswith(".pth") or "__editable__" in name:
            return f"start-up hook {name}"
    return None


def _install_reviewed_wheel(root: Path, package: str, dist: str) -> None:
    """Build the source to a wheel in a scratch directory, check its file list,
    then install that wheel — never ``pip install <source>`` straight into the venv."""
    import subprocess
    import tempfile
    import zipfile
    from importlib.metadata import packages_distributions
    from packaging.utils import canonicalize_name
    from core.lazy_installer import child_env
    owners = packages_distributions().get(package) or []
    if any(canonicalize_name(o) != canonicalize_name(dist or "") for o in owners):
        raise click.ClickException(f"package {package!r} already belongs to "
                                   f"{', '.join(sorted(owners))}; a pack cannot replace it")
    with tempfile.TemporaryDirectory(prefix="polyrob-pack-") as out:
        cmd = [sys.executable, "-I", "-m", "pip", "wheel", "--disable-pip-version-check",
               "--no-deps", "-w", out, str(root)]
        proc = subprocess.run(cmd, env=child_env(), capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise click.ClickException(f"pack build failed:\n{(proc.stderr or proc.stdout)[-2000:]}")
        wheels = sorted(Path(out).glob("*.whl"))
        if len(wheels) != 1:
            raise click.ClickException(f"pack build produced {len(wheels)} wheels; expected one")
        with zipfile.ZipFile(wheels[0]) as zf:
            reason = wheel_refusal(zf.namelist(), package, dist)
        if reason:
            raise click.ClickException(f"pack wheel refused: {reason}")
        _pip(["--no-deps", str(wheels[0])])


def _install_third_party(root: Path, origin: str, accepted: bool) -> None:
    _refuse_under_custody()
    import tomllib
    from core.packs import index
    from core.packs.manifest import ManifestError, parse
    path = _find_manifest(root)
    try:
        m = parse(tomllib.loads(path.read_text(encoding="utf-8")), path)
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError, ManifestError) as exc:
        raise click.ClickException(f"not an installable pack: {type(exc).__name__}: {exc}")
    if m.tier != "third-party":
        raise click.ClickException("this source declares tier first-party; first-party packs "
                                   "install from the index (polyrob pack install <id>)")
    eps = project.get("entry-points", {}).get("polyrob.packs", {})
    if m.id not in eps:
        raise click.ClickException(f"pyproject declares no polyrob.packs entry point {m.id!r}")
    if set(eps) != {m.id}:
        # A second entry point would install a pack whose manifest was never shown.
        raise click.ClickException(f"pyproject declares more than one polyrob.packs entry "
                                   f"point ({', '.join(sorted(eps))}); a pack declares exactly one")
    if index.find(m.id) is not None:
        raise click.ClickException(f"pack id {m.id!r} belongs to an index pack")
    dist, version = project.get("name", ""), project.get("version", "")
    # A third-party source must not take a core-reviewed DISTRIBUTION name: pip
    # would replace polyrob itself or an index pack's files with this source's
    # (the loader's first-party identity check then passes on attacker code).
    from packaging.utils import canonicalize_name
    # 067: and never a retired first-party pack name (index.RETIRED_DISTS): the
    # loader never loads one, and a squatter's upload of that name must not be
    # installable through this command either.
    reserved = ({canonicalize_name("polyrob")} | {canonicalize_name(r["dist"]) for r in index.rows()}
                | set(index.RETIRED_DISTS))
    if canonicalize_name(dist or "") in reserved:
        raise click.ClickException(f"distribution {dist!r} belongs to core, an index pack or a "
                                   "retired first-party pack name; a third-party pack cannot "
                                   "replace it")
    killed = index.killed(m.id, m.version) or index.killed(dist, version or m.version)
    if killed:
        raise click.ClickException(f"pack {m.id} {m.version} is refused: {killed}")
    click.echo(f"third-party pack {m.id} {m.version} ({dist}) from {origin}")
    click.echo(f"summary: {m.summary or '-'}")
    click.echo(f"declared capabilities: {', '.join(sorted(m.capabilities)) or 'none'}")
    for tool in m.tools:
        click.echo(f"  tool {tool.id}: {', '.join(sorted(tool.row)) or 'no special capabilities'}"
                   f" · actions: {', '.join(v.name for v in tool.verbs)}")
    click.echo("It runs inside the agent process and can read what the agent can read.")
    _accept(accepted)
    _install_reviewed_wheel(root, path.parent.name, dist)
    deps = project.get("dependencies", [])
    click.echo(f"pack {m.id} installed (--no-deps).")
    if deps:
        click.echo("It declares these dependencies, NOT installed:")
        for dep in deps:
            click.echo(f"  {dep}")
        # Never print an unpinned, unhashed install line: review each dependency,
        # pin it to an exact version with its wheel hash, then install the file.
        click.echo("review each one, pin it as `name==version --hash=sha256:<wheel hash>` in a "
                   f"file, then: {sys.executable} -I -m pip install --require-hashes "
                   "--no-deps -r <that file>")
    click.echo(f"load it in a new process with: polyrob pack enable {m.id}")


@pack.command("remove")
@click.argument("pack_id")
def pack_remove(pack_id):
    """Print the command that removes a pack (removal is never automatic here)."""
    _valid_id(pack_id)
    state = _loaded_state()
    rec = state.record(pack_id)
    if rec is None:
        raise click.ClickException(f"pack {pack_id!r} is not installed")
    if rec.dist_name and rec.tier == "first-party":
        # 067 (one install): it ships inside polyrob — nothing to uninstall.
        click.echo(f"pack {pack_id} ships inside polyrob and cannot be uninstalled on its own.")
        click.echo(f"turn it off with: polyrob pack disable {pack_id}")
        sys.exit(1)
    dist = rec.dist_name
    if not dist:
        from importlib.metadata import packages_distributions
        dists = packages_distributions().get(rec.entry_point.split(":")[0].split(".")[0])
        dist = dists[0] if dists else ""
    click.echo(f"remove pack {pack_id} with:")
    click.echo(f"  {sys.executable} -m pip uninstall {dist or '<its distribution>'}")
    click.echo(f"or keep it installed and run: polyrob pack disable {pack_id}")
    sys.exit(1)


class PackCommand(click.Command):
    """Stand-in for a CLI command a pack declares (``[cli] commands``). Resolving
    it imports nothing; invoking it loads env + packs (after the root group has
    activated the profile) and runs the pack's real command, or names why the
    pack cannot run."""

    def __init__(self, name: str, pack_id: str):
        super().__init__(name, help=f"(pack {pack_id})", add_help_option=False,
                         context_settings={"ignore_unknown_options": True,
                                           "allow_extra_args": True})
        self.pack_id = pack_id

    def invoke(self, ctx):
        state = _loaded_state()
        rec = state.record(self.pack_id)
        real = rec.commands.get(self.name) if rec is not None and rec.status == state.LOADED else None
        if real is None:
            why = rec.line() if rec is not None else f"pack {self.pack_id} is not installed"
            raise click.ClickException(f"`{self.name}` comes from pack {self.pack_id}: {why}")
        sub = real.make_context(self.name, list(ctx.args), parent=ctx.parent)
        with sub:
            return real.invoke(sub)
