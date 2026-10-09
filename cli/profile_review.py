"""Bounded, inert snapshots for reviewing executable profile distributions."""
import hashlib
import os
import stat
from pathlib import Path

import click

from core.security.workspace_io import read_bytes

MAX_FILES = 1000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def snapshot(source: Path, destination: Path) -> Path:
    if source.is_symlink():
        raise click.ClickException("profile source must not be a symlink")
    destination.mkdir(mode=0o700)
    count = total = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in dirs + sorted(files):
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(source)
            if stat.S_ISDIR(info.st_mode):
                (destination / relative).mkdir(mode=0o700)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise click.ClickException(f"profile contains a link or special file: {relative}")
            count += 1
            if count > MAX_FILES:
                raise click.ClickException("profile contains too many files")
            raw = read_bytes(path, source, max_bytes=MAX_FILE_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise click.ClickException("profile exceeds the review size limit")
            target = destination / relative
            target.write_bytes(raw)
            target.chmod(0o700 if info.st_mode & 0o111 else 0o600)
    return destination


def inventory(source: Path) -> tuple[str, list[str]]:
    digest = hashlib.sha256()
    paths = sorted(p for p in source.rglob("*") if p.is_file())
    names = []
    for path in paths:
        name = path.relative_to(source).as_posix()
        raw = path.read_bytes()
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(bytes([bool(path.stat().st_mode & 0o111)]))
        digest.update(len(raw).to_bytes(8, "big") + raw)
        names.append(name)
    return digest.hexdigest(), names


_SHOW_BYTES = 4000


def _safe(text: str) -> str:
    """Terminal-safe display text: no escape sequences or other control bytes."""
    return "".join(c if c in "\n\t" or (" " <= c and c != "\x7f" and not "\x80" <= c <= "\x9f")
                   else "?" for c in text)


def _show(path: Path) -> list[str]:
    raw = path.read_bytes()
    text = _safe(raw[:_SHOW_BYTES].decode("utf-8", errors="replace"))
    lines = ["    | " + line for line in text.splitlines()]
    if len(raw) > _SHOW_BYTES:
        lines.append(f"    | … ({len(raw) - _SHOW_BYTES} more bytes)")
    return lines


def _mcp_lines(path: Path) -> list[str]:
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("servers") or data.get("mcpServers") or {}
        if not isinstance(servers, dict):
            raise ValueError("servers is not an object")
    except Exception:
        return ["  mcp.json (could not parse; raw content):"] + _show(path)
    out = [f"  mcp.json: {len(servers)} MCP server(s) — each command RUNS on this host:"]
    for name, spec in sorted(servers.items()):
        spec = spec if isinstance(spec, dict) else {}
        cmd = " ".join(str(x) for x in [spec.get("command") or ""] + list(spec.get("args") or []))
        out.append(f"    - {_safe(str(name))}: command={_safe(cmd.strip()) or '-'}"
                   f" url={_safe(str(spec.get('url') or '-'))}"
                   f" env={','.join(sorted(_safe(str(k)) for k in (spec.get('env') or {}))) or '-'}")
    return out


def _env_requires_lines(source: Path) -> list[str]:
    """The env keys the manifest asks for — named BEFORE the digest is approved,
    so a profile cannot quietly ask for a wallet seed or another credential."""
    manifest = source / "polyrob.profile.yaml"
    if not manifest.is_file():
        return []
    try:
        import yaml
        reqs = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("env_requires")
    except Exception:
        return ["  env_requires: (manifest could not be parsed; see the raw manifest below)"]
    if not isinstance(reqs, list) or not reqs:
        return ["  env_requires: none"]
    out = [f"  env_requires ({len(reqs)} key(s) this profile asks you to set):"]
    for r in reqs:
        r = r if isinstance(r, dict) else {"name": r}
        name = _safe(str(r.get("name") or "").strip()) or "?"
        need = "required" if r.get("required") else "optional"
        out.append(f"    - {name} ({need})")
    return out


def scan_skills(source: Path) -> None:
    """The skill_install threat scan, fail-closed, over the profile's skills."""
    skills = source / "skills"
    if not skills.is_dir():
        return
    from cli.commands.skill_install import InstallError, _scan_folder
    try:
        _scan_folder(skills)
    except InstallError as exc:
        raise click.ClickException(f"profile skills refused: {exc.message}")


def review_report(source: Path, owned) -> list[str]:
    """What this distribution can DO, shown before the owner approves its digest:
    MCP commands, cron/goal prompts, config, soul, skills and env needs."""
    out = ["Profile review — content that grants authority:"]
    out += _env_requires_lines(source)
    for rel in owned:
        path = source / rel
        if not path.exists():
            continue
        if rel == "mcp.json" and path.is_file():
            out += _mcp_lines(path)
        elif rel == "skills" and path.is_dir():
            names = sorted(p.name for p in path.iterdir() if p.is_dir())
            out.append(f"  skills ({len(names)}, threat-scanned): {', '.join(names) or '-'}")
        elif path.is_dir():
            for f in sorted(q for q in path.rglob("*") if q.is_file()):
                out.append(f"  {f.relative_to(source).as_posix()}:")
                out += _show(f)
        elif rel != "polyrob.profile.yaml":
            out.append(f"  {rel}:")
            out += _show(path)
    for rel in ("config.yaml", "soul.md", "polyrob.profile.yaml"):
        if (source / rel).is_file():
            out.append(f"  {rel}:")
            out += _show(source / rel)
    executable = [p.relative_to(source).as_posix() for p in sorted(source.rglob("*"))
                  if p.is_file() and p.stat().st_mode & 0o111]
    if executable:
        out.append("  executable files: " + ", ".join(executable))
    return out


def changed_paths(source: Path, installed: Path, owned) -> list[str]:
    """Owned files that an update adds, changes or removes (relative paths)."""
    def files(root: Path) -> dict:
        found = {}
        for rel in list(owned) + ["config.yaml"]:
            base = root / rel
            if base.is_file():
                found[rel] = base.read_bytes()
            elif base.is_dir():
                for f in base.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        found[f.relative_to(root).as_posix()] = f.read_bytes()
        return found
    new, old = files(source), files(installed)
    return sorted(k for k in set(new) | set(old) if new.get(k) != old.get(k))


def require_review(source: Path, expected: str | None, *, owned=(),
                   installed: Path | None = None) -> str:
    scan_skills(source)
    digest, names = inventory(source)
    if not expected or expected.lower() != digest:
        # No activation occurs until the owner supplies the reviewed digest, and
        # the digest is printed only AFTER the content it approves.
        for line in review_report(source, owned or ()):
            click.echo(line, err=True)
        if installed is not None:
            changed = changed_paths(source, installed, owned or ())
            click.echo("  changed since the installed version: "
                       + (", ".join(changed) or "nothing"), err=True)
        raise click.ClickException(
            "Profile content has not been approved. Read the review above "
            "(MCP commands run on this host; cron and goal prompts run under your keys), "
            f"then pass --sha256 {digest}. Files: " + ", ".join(names))
    return digest
