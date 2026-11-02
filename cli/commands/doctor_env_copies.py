"""``polyrob doctor``: env-file COPIES that hold a wallet key.

The custody check reads the env files this process loads and the
``polyrob update`` snapshots. An operator's own backup (``.env.bak-2026…``,
``.env.orig``, ``polyrob.env.save``) holds the seed too, and code that runs on
the host as this user can read it (2026-10-29). Doctor names such a copy; it
never reads the value out and never edits the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import List


def _loaded_env_files() -> set:
    out = set()
    try:
        from core.paths import env_file_candidates
        for c in env_file_candidates():
            try:
                out.add(Path(c.path).resolve())
            except OSError:
                continue
    except Exception:
        pass
    return out


def _dirs(env: dict) -> List[Path]:
    from core.paths import polyrob_home
    dirs = [polyrob_home()]
    try:
        from core.runtime_paths import effective_data_home
        dirs.append(effective_data_home())
    except Exception:
        pass
    explicit = (env.get("POLYROB_DATA_DIR") or "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(Path("/etc/polyrob"))
    seen, out = set(), []
    for d in dirs:
        try:
            key = d.resolve()
        except OSError:
            continue
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


def env_copy_lines(env: dict) -> List[str]:
    from core.security.custody_env import file_holds_custody_secret

    loaded = _loaded_env_files()
    found: List[str] = []
    for d in _dirs(env):
        try:
            names = sorted(p for p in d.iterdir() if p.is_file() and ".env" in p.name)
        except OSError:
            continue
        for p in names:
            try:
                if p.resolve() in loaded or p.name in (".env", "polyrob.env"):
                    continue
            except OSError:
                continue
            if file_holds_custody_secret(p):
                found.append(str(p))
    if not found:
        return []
    return [f"! env file copy holds a wallet key: {', '.join(found)} — code that runs on "
            "the host as this user can read it; delete the copy (keep one offline backup "
            "from `polyrob wallet export`)"]
